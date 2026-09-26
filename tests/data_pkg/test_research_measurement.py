# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R-D — the outbound-research program's three counters, DB-free.

The whole point of these tests is the arithmetic's HONESTY, not its speed: a
counter that reports ``0.0`` where it means ``unknown`` would make the research
program unfalsifiable, which is the exact failure RESEARCH_PROGRAM_SPEC §4 was
written to prevent. So every honest-null path has its own case — an empty
window, an under-powered window (gate G9), an unmatured corroboration window,
and the one that is easiest to get wrong: rows that exist but carry no
write-time novelty stamp, which must read as ``novelty_not_stamped`` and NEVER
as a 0% novelty rate.

The IL-SHAPED CASE at the bottom is the spec's own worked example — the
coverage-floor alert that actually fired (``country_watch_il`` / Palestine,
2026-09-04) carried end to end: a research row dispatched by that target, cited
by one desk head, that head quoted into a composition, and the row corroborated
by a later non-research wire item. It is the one case where all three counters
fire at once, and it pins their interaction rather than each in isolation.

Ephemeral-DB lifecycle through the real binding path lives in
``test_research_measurement_db.py``.
"""
from __future__ import annotations

import pytest

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import research_measurement as rm
from legba.data.analysts.handler_options import known_option_names
from legba.data.provenance.kinds import (
    STRUCTURAL_VERIFY_EXEMPT_ANALYSTS,
    OutputKind,
)


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_registered_as_finding_sub_handler_and_structural_exempt():
    """§4.4 — the counters publish as ``kind='finding'`` +
    ``analyst_id='research_measurement'`` (the calibration_tracking precedent,
    NO new OutputKind), so the handler must also sit in the structural-exempt
    registry the FINDING-set drift guard asserts equality against."""
    assert SUB_HANDLERS["research_measurement"] is rm.handle
    assert OUTPUT_KIND_BY_SUB_HANDLER["research_measurement"] is OutputKind.FINDING
    assert "research_measurement" in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS


def test_every_knob_is_declared_in_the_x1_catalog():
    """A knob the catalog does not declare is unreachable dead config."""
    assert set(known_option_names("research_measurement")) == {
        "window_days",
        "maturation_days",
        "corroboration_lookback_hours",
        "min_n",
        "weak_entity_overlap",
        "row_cap",
        "head_cap",
    }


async def test_refuses_loud_without_pool():
    """Zero is ALSO this counter's honest verified baseline, so a zeroed
    finding emitted because the pool was missing would be indistinguishable
    from the real measurement. It raises instead."""
    with pytest.raises(RuntimeError, match="pg_pool"):
        await rm.handle([], {"sub_handler": "research_measurement"}, None)


def test_the_origin_predicate_is_the_migration_0112_label():
    """The counters key on the one column that distinguishes a research signal
    (§1.1). If this prefix ever drifts from ``retrieval_origin.py``'s, every
    counter silently measures an empty set."""
    from legba.data.retrieval_origin import WEB_SEARCH_PREFIX, web_search_origin

    assert rm._ORIGIN_LIKE == f"{WEB_SEARCH_PREFIX}%"
    origin = web_search_origin("search.searxng.local")
    assert origin.startswith(rm._ORIGIN_LIKE[:-1])


# ---------------------------------------------------------------------------
# rate() — gate G9, and the whole honest-null discipline in one function
# ---------------------------------------------------------------------------


def test_rate_is_null_with_a_reason_on_an_empty_denominator():
    block = rm.rate(0, 0, min_n=10)
    assert block == {"rate": None, "n": 0, "count": 0, "reason": "no_rows"}


def test_rate_is_null_below_the_g9_floor_and_still_publishes_its_n():
    """G9: ``n < min_n`` publishes null + a reason, NEVER a number — and still
    carries the counts, so a reader can see how close to powered it is."""
    block = rm.rate(3, 4, min_n=10)
    assert block["rate"] is None
    assert block["reason"] == "n_below_minimum"
    assert (block["n"], block["count"]) == (4, 3)


def test_rate_is_earned_at_or_above_the_floor():
    block = rm.rate(3, 12, min_n=10)
    assert block == {"rate": 0.25, "n": 12, "count": 3, "reason": None}


def test_a_zero_rate_is_a_real_measurement_not_a_missing_one():
    """The inverse of the honest-null rule: 0.0 must be reachable, and must
    mean 'we looked at n>=min_n rows and none qualified'."""
    block = rm.rate(0, 20, min_n=10)
    assert block["rate"] == 0.0
    assert block["reason"] is None


# ---------------------------------------------------------------------------
# NOVELTY (§4.1) — including F-9's over-reporting caveat
# ---------------------------------------------------------------------------


def _nrow(**kw):
    row = {
        "signal_id": kw.pop("signal_id", "s"),
        "retrieval_origin": "web_search:search.searxng.local",
        "target_id": "country_watch_il",
        "day": "2026-09-05",
        "novel": None,
        "host_in_slice": None,
        "scope": "dispatching_target",
        "novelty_version": "novelty.v1",
        "folded_onto_curated": False,
    }
    row.update(kw)
    return row


def test_novelty_empty_window_is_null_not_zero():
    block = rm.novelty_counters([], min_n=10)
    assert block["rate"] is None
    assert block["reason"] == "no_rows"
    assert block["n"] == 0


def test_novelty_unstamped_rows_are_unknown_not_not_novel():
    """THE case this handler exists to get right. Twelve research rows landed
    and R-A stamped none of them: the denominator is empty, so the rate is null
    with ``novelty_not_stamped``. Counting them as 'not novel' would publish a
    0.0 novelty rate for a program that had simply not been measured."""
    rows = [_nrow(signal_id=f"s{i}") for i in range(12)]
    block = rm.novelty_counters(rows, min_n=10)
    assert block["rate"] is None
    assert block["reason"] == "novelty_not_stamped"
    assert block["unstamped"] == 12
    assert block["n"] == 0


def test_novelty_counts_only_stamped_rows_in_its_denominator():
    """A partially-stamped window measures the stamped part and says how much
    it could not see."""
    rows = [_nrow(signal_id=f"n{i}", novel=True) for i in range(8)]
    rows += [_nrow(signal_id=f"o{i}", novel=False) for i in range(4)]
    rows += [_nrow(signal_id=f"u{i}") for i in range(5)]  # unstamped
    block = rm.novelty_counters(rows, min_n=10)
    assert block["n"] == 12
    assert block["count"] == 8
    assert block["rate"] == pytest.approx(0.6667, abs=1e-4)
    assert block["unstamped"] == 5


def test_novelty_v1b_is_the_stricter_arm_and_has_its_own_denominator():
    """v1b = novel AND the publisher's host was not already in the slice. A row
    that stamped ``novel`` but not ``host_in_slice`` cannot answer the stricter
    question, so it is EXCLUDED from v1b's denominator rather than assumed."""
    rows = [
        _nrow(signal_id=f"a{i}", novel=True, host_in_slice=False)
        for i in range(6)
    ]
    rows += [
        _nrow(signal_id=f"b{i}", novel=True, host_in_slice=True)
        for i in range(6)
    ]
    rows += [_nrow(signal_id="c0", novel=True)]  # host_in_slice unstamped
    block = rm.novelty_counters(rows, min_n=10)
    assert block["n"] == 13          # v1 sees all thirteen stamped rows
    assert block["rate"] == 1.0      # every one of them is "novel" under v1
    v1b = block["v1b"]
    assert v1b["n"] == 12            # the unstamped-host row is excluded
    assert v1b["count"] == 6
    assert v1b["rate"] == 0.5        # v1's 1.0 was an over-report (F-9)


def test_novelty_publishes_the_near_dup_companion_and_never_merges_it():
    """F-9: v1 over-reports. The conservative companion is the share the dedup
    plane later folded onto a PRE-EXISTING non-research row. Both numbers are
    published; the truth is between them, and the caveat says so."""
    rows = [_nrow(signal_id=f"a{i}", novel=True) for i in range(10)]
    rows += [
        _nrow(signal_id=f"d{i}", novel=True, folded_onto_curated=True)
        for i in range(5)
    ]
    block = rm.novelty_counters(rows, min_n=10)
    assert block["rate"] == 1.0
    assert block["near_dup"]["rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert block["near_dup"]["n"] == 15
    assert "OVER-REPORTS" in block["caveat"]
    # The two must remain separate keys — never one blended number.
    assert block["near_dup"]["rate"] != block["rate"]


def test_novelty_ignores_self_selected_runs_and_says_how_many():
    """§1.3 / F-8: a self-selected run has no dispatching target, so it has no
    slice to be novel against. Those rows leave the denominator and are
    counted, not silently dropped."""
    rows = [_nrow(signal_id=f"a{i}", novel=True) for i in range(11)]
    rows += [_nrow(signal_id="x", novel=True, scope="none")]
    block = rm.novelty_counters(rows, min_n=10)
    assert block["n"] == 11
    assert block["scope_filtered_out"] == 1


# ---------------------------------------------------------------------------
# CORROBORATION (§4.2)
# ---------------------------------------------------------------------------


def _crow(**kw):
    row = {
        "signal_id": kw.pop("signal_id", "s"),
        "retrieval_origin": "web_search:search.searxng.local",
        "target_id": "country_watch_il",
        "day": "2026-08-29",
        "anchor_fold": False,
        "anchor_url": False,
        "anchor_fact": False,
        "anchor_entity": False,
    }
    row.update(kw)
    return row


def test_corroboration_unmatured_window_says_so():
    """§4.2's maturation lag: a signal fetched today cannot be scored for seven
    days. Before then the answer is ``window_not_matured``, which is a
    different statement from 'nothing corroborated it'."""
    block = rm.corroboration_counters([], min_n=10, matured=False)
    assert block["rate"] is None
    assert block["reason"] == "window_not_matured"
    assert block["matured"] is False


def test_corroboration_strong_arm_unions_fold_and_url_only():
    rows = [_crow(signal_id=f"f{i}", anchor_fold=True) for i in range(4)]
    rows += [_crow(signal_id=f"u{i}", anchor_url=True) for i in range(2)]
    rows += [_crow(signal_id=f"e{i}", anchor_entity=True) for i in range(4)]
    rows += [_crow(signal_id=f"n{i}") for i in range(2)]
    block = rm.corroboration_counters(rows, min_n=10, matured=True)
    assert block["n"] == 12
    assert block["count"] == 6                  # fold + url, NOT entity
    assert block["by_anchor"] == {"fold": 4, "url": 2, "fact": 0, "entity": 4}
    assert block["anchors_strong"] == ["fold", "url"]


def test_corroboration_weak_arm_is_separate_and_never_lifts_a_ceiling():
    """The entity-overlap arm is the WEAK companion §4.2 names. It gets its own
    rate and must never be unioned into the strong one."""
    rows = [_crow(signal_id=f"e{i}", anchor_entity=True) for i in range(12)]
    block = rm.corroboration_counters(rows, min_n=10, matured=True)
    assert block["rate"] == 0.0        # strong arm: genuinely nothing
    assert block["weak"]["rate"] == 1.0
    assert "never used to lift a ceiling" in block["weak"]["definition"]


def test_this_gauge_never_lifts_a_ceiling_and_says_so_on_every_row():
    """The ceiling lift (§1.4b) is a WRITE and is deliberately not built here.
    It is NOT stubbed: the row states plainly that no lift was performed, so a
    reader can never mistake the counter's silence for a lift."""
    block = rm.corroboration_counters(
        [_crow(anchor_fold=True)], min_n=10, matured=True
    )
    assert block["ceiling_lift"]["performed"] is False
    assert block["ceiling_lift"]["reason"] == "not_built_read_only_gauge"


def test_the_fact_anchor_zero_is_explained_not_merely_absent():
    """§1.4c keeps research signals out of fact_extractor until corroborated,
    so the ``fact`` anchor is structurally zero under regime 1. Publishing the
    zero WITH its reason is the difference between a measurement and a hole."""
    block = rm.corroboration_counters([], min_n=10, matured=True)
    assert block["by_anchor"]["fact"] == 0
    assert "STRUCTURALLY ZERO" in block["anchor_fact_note"]


# ---------------------------------------------------------------------------
# CONSEQUENCE (§4.3)
# ---------------------------------------------------------------------------


def _qrow(**kw):
    row = {
        "signal_id": kw.pop("signal_id", "s"),
        "retrieval_origin": "web_search:search.searxng.local",
        "target_id": "country_watch_il",
        "day": "2026-09-05",
        "c1_cited_any": False,
        "c2_cited_by_desk": False,
        "c3_quoted_head": False,
        "desk_pairs": [],
    }
    row.update(kw)
    return row


def test_consequence_ladder_ascends_and_is_labelled_reach_not_effect():
    rows = [_qrow(signal_id=f"a{i}", c1_cited_any=True) for i in range(6)]
    rows += [
        _qrow(signal_id=f"b{i}", c1_cited_any=True, c2_cited_by_desk=True)
        for i in range(3)
    ]
    rows += [
        _qrow(
            signal_id="c0",
            c1_cited_any=True,
            c2_cited_by_desk=True,
            c3_quoted_head=True,
        )
    ]
    rows += [_qrow(signal_id=f"z{i}") for i in range(2)]
    block = rm.consequence_counters(rows, min_n=10)
    assert (block["c1_cited_any"], block["c2_cited_by_desk"]) == (10, 4)
    assert block["c3_quoted_head"] == 1
    assert block["n"] == 12
    assert block["c1"]["rate"] > block["c2"]["rate"] > block["c3"]["rate"]
    assert block["ladder"] == "reach"
    assert "never of EFFECT" in block["ladder_note"]


def test_consequence_is_honest_null_on_an_empty_window():
    block = rm.consequence_counters([], min_n=10)
    assert block["n"] == 0
    for rung in ("c1", "c2", "c3"):
        assert block[rung]["rate"] is None
        assert block[rung]["reason"] == "no_rows"


# ---------------------------------------------------------------------------
# The published tables — two grains, never pooled
# ---------------------------------------------------------------------------


def test_per_day_reports_an_absent_day_as_absent():
    """No zero-filling: a day with no research signal simply has no row. A
    fabricated zero day would drag every daily rate toward nothing."""
    rows = [_nrow(signal_id="a", day="2026-09-01", novel=True),
            _nrow(signal_id="b", day="2026-09-03", novel=True)]
    series = rm.per_day(rows, [], min_n=10)
    assert [r["day"] for r in series] == ["2026-09-01", "2026-09-03"]
    # One signal a day is far under the G9 floor, so each day's rate is null.
    assert all(r["novelty"]["rate"] is None for r in series)


def test_per_target_keys_self_selected_runs_separately():
    rows = [_nrow(signal_id="a", target_id=None, novel=True),
            _nrow(signal_id="b", target_id="country_watch_il", novel=True)]
    table = rm.per_target(rows, [], min_n=10, matured=False)
    assert {r["target_id"] for r in table} == {
        "(self_selected)", "country_watch_il"
    }


def test_per_target_unit_uses_the_citing_grain_not_the_dispatch_grain():
    """The two tables count different things: ``by_target`` divides by the
    DISPATCH denominator, ``by_target_unit`` by the CITING one. Mixing them
    would divide a desk's citations by a dispatch count."""
    sep = rm._PAIR_SEP
    rows = [
        _qrow(
            signal_id="s1",
            target_id="country_watch_il",
            c2_cited_by_desk=True,
            c3_quoted_head=True,
            desk_pairs=[f"internal_stability{sep}country_watch_il"],
        ),
        _qrow(
            signal_id="s2",
            target_id="country_watch_il",
            c2_cited_by_desk=True,
            desk_pairs=[f"escalation{sep}country_g20_tr"],
        ),
    ]
    table = rm.per_target_unit(rows, min_n=10)
    assert {(r["target_id"], r["unit"]) for r in table} == {
        ("country_watch_il", "internal_stability"),
        ("country_g20_tr", "escalation"),
    }
    quoted = [r for r in table if r["unit"] == "internal_stability"][0]
    assert quoted["quoted_into_composition"] == 1
    assert quoted["share_of_window"]["rate"] is None   # n=2, under G9


# ---------------------------------------------------------------------------
# §4.5 — the R4 stratification
# ---------------------------------------------------------------------------


def _head(**kw):
    row = {
        "head_id": kw.pop("head_id", "h"),
        "unit": "internal_stability",
        "target_id": "country_watch_il",
        "citation_count": 3,
        "has_research_evidence": False,
    }
    row.update(kw)
    return row


def test_stratification_splits_two_arms_and_refuses_to_pool():
    rows = [_head(head_id=f"t{i}", has_research_evidence=True) for i in range(2)]
    rows += [
        _head(head_id=f"f{i}", unit="escalation", target_id="country_g20_tr")
        for i in range(5)
    ]
    strat = rm.stratification(rows)
    assert strat["axis"] == "has_research_evidence"
    assert strat["pooled"] is False
    assert strat["arms"]["true"]["n_heads"] == 2
    assert strat["arms"]["false"]["n_heads"] == 5
    assert strat["arms"]["true"]["n_units"] == 1
    assert strat["arms"]["false"]["n_targets"] == 1


def test_an_uncited_head_stays_in_the_false_arm_and_is_counted():
    """A head that cited nothing genuinely cited no research evidence, so it
    belongs in the ``false`` arm — but R4 must be able to see how much of that
    arm is 'cited nothing at all' rather than 'cited only curated evidence'."""
    rows = [_head(head_id="a", citation_count=0),
            _head(head_id="b", citation_count=4)]
    arms = rm.stratification(rows)["arms"]
    assert arms["false"]["n_heads"] == 2
    assert arms["false"]["n_uncited_heads"] == 1
    assert arms["true"]["n_heads"] == 0


def test_the_stratification_sql_is_the_documented_one():
    """§4.5's query is reproduced in the module docstring so the R4 contract is
    readable without a query planner. Keep the two in step."""
    for fragment in (
        "has_research_evidence",
        "bounded_question",
        "jsonb_array_length(h.citations)",
    ):
        assert fragment in rm.STRATIFY_HEADS_SQL
        assert fragment in rm.__doc__


# ---------------------------------------------------------------------------
# The published row
# ---------------------------------------------------------------------------


def _payload(**kw):
    base = {
        "schema": rm.SCHEMA,
        "regime": "substrate",
        "window_days": 7,
        "min_n": 10,
        "signals_written": 0,
        "retrieval_origins": [],
        "novelty": rm.novelty_counters([], min_n=10),
        "corroboration": rm.corroboration_counters(
            [], min_n=10, matured=False
        ),
        "consequence": rm.consequence_counters([], min_n=10),
        "per_day": [],
        "stratification": rm.stratification([]),
    }
    base.update(kw)
    return base


def test_the_finding_is_marked_meta_so_no_desk_can_read_it_back_in():
    """§4.4. ``window_ledger``, ``composition_window`` and
    ``meta_findings_synthesizer`` all exclude on exactly this key — without it
    a measurement row could be composed into a read it is measuring."""
    finding = rm.build_finding(_payload())
    assert finding.data["meta"] is True
    assert finding.data["sub_handler"] == "research_measurement"
    assert finding.data["research_measurement"]["schema"] == rm.SCHEMA


def test_the_body_prints_the_reason_when_a_rate_is_null():
    """A reader must never see a blank where a number was refused."""
    finding = rm.build_finding(_payload())
    assert "NOVELTY        null (n=0, no_rows)" in finding.body
    assert "null (n=0, window_not_matured)" in finding.body


def test_the_regime_flag_rides_on_the_row_and_its_tags():
    """§4.5 / §6.2 — every counter row carries the flag value so a pooling
    reader can re-split across the cutover retroactively."""
    finding = rm.build_finding(_payload(regime="desks"))
    assert "research_regime:desks" in finding.tags
    assert finding.data["research_measurement"]["regime"] == "desks"


def test_an_unset_flag_is_reported_as_unset_not_as_off(monkeypatch):
    """Before R-A merges there is no write path at all. Claiming the documented
    default ``off`` would assert something about a path that does not exist."""
    monkeypatch.delenv(rm.RESEARCH_EVIDENCE_ENV, raising=False)
    assert rm._regime()["regime"] == rm.REGIME_UNSET
    assert rm._regime()["regime_known"] is False
    monkeypatch.setenv(rm.RESEARCH_EVIDENCE_ENV, "substrate")
    assert rm._regime() == {
        "regime": "substrate",
        "regime_known": True,
        "regime_env": rm.RESEARCH_EVIDENCE_ENV,
    }


# ---------------------------------------------------------------------------
# THE IL-SHAPED CASE — the spec's own worked example, all three counters at once
# ---------------------------------------------------------------------------


def test_the_il_shaped_case_fires_all_three_counters():
    """``country_watch_il`` / Palestine — the ONE coverage-floor alert that has
    ever fired (2026-09-04, slice_share 0.1998 over 235 signals).

    The shape: the alert dispatches a research run against that target; the run
    lands a page absent from the IL slice; one desk head (internal_stability on
    country_watch_il) cites it; that head is quoted into the Morning Read; and a
    non-research wire item carrying the same observation lands two days later
    and folds onto it.

    Each counter is under G9 on its own — which is exactly F-10's point, and
    the reason all three rates are null here while their COUNTS are real. This
    test pins that combination: the events happened, and the rates still refuse
    to be quoted.
    """
    sep = rm._PAIR_SEP
    novelty_rows = [
        _nrow(
            signal_id="il-research-1",
            target_id="country_watch_il",
            day="2026-09-04",
            novel=True,
            host_in_slice=False,
        )
    ]
    corroboration_rows = [
        _crow(
            signal_id="il-research-1",
            target_id="country_watch_il",
            day="2026-09-04",
            anchor_fold=True,          # the later wire row folded onto it
        )
    ]
    consequence_rows = [
        _qrow(
            signal_id="il-research-1",
            target_id="country_watch_il",
            day="2026-09-04",
            c1_cited_any=True,
            c2_cited_by_desk=True,
            c3_quoted_head=True,
            desk_pairs=[f"internal_stability{sep}country_watch_il"],
        )
    ]

    novelty = rm.novelty_counters(novelty_rows, min_n=10)
    corroboration = rm.corroboration_counters(
        corroboration_rows, min_n=10, matured=True
    )
    consequence = rm.consequence_counters(consequence_rows, min_n=10)

    # The EVENTS are real and counted...
    assert novelty["count"] == 1 and novelty["n"] == 1
    assert novelty["v1b"]["count"] == 1          # host was not in the slice
    assert corroboration["count"] == 1
    assert corroboration["by_anchor"]["fold"] == 1
    assert consequence["c3_quoted_head"] == 1

    # ...and NOT ONE of the three rates is quotable at n=1 (gate G9 / F-10).
    assert novelty["rate"] is None
    assert corroboration["rate"] is None
    assert consequence["c2"]["rate"] is None
    for block in (novelty, corroboration, consequence["c2"]):
        assert block["reason"] == "n_below_minimum"

    # The two grains stay apart: dispatch target vs citing (target, unit).
    by_target = rm.per_target(
        novelty_rows, corroboration_rows, min_n=10, matured=True
    )
    by_target_unit = rm.per_target_unit(consequence_rows, min_n=10)
    assert [r["target_id"] for r in by_target] == ["country_watch_il"]
    assert [(r["target_id"], r["unit"]) for r in by_target_unit] == [
        ("country_watch_il", "internal_stability")
    ]
    assert by_target_unit[0]["quoted_into_composition"] == 1


def test_the_il_case_reaches_the_published_row_intact():
    """The same case rendered as the finding an operator actually reads."""
    sep = rm._PAIR_SEP
    novelty_rows = [
        _nrow(signal_id="il-1", novel=True, host_in_slice=False,
              day="2026-09-04")
    ]
    consequence_rows = [
        _qrow(signal_id="il-1", c1_cited_any=True, c2_cited_by_desk=True,
              c3_quoted_head=True, day="2026-09-04",
              desk_pairs=[f"internal_stability{sep}country_watch_il"])
    ]
    payload = _payload(
        signals_written=1,
        retrieval_origins=["web_search:search.searxng.local"],
        novelty=rm.novelty_counters(novelty_rows, min_n=10),
        consequence=rm.consequence_counters(consequence_rows, min_n=10),
        per_day=rm.per_day(novelty_rows, consequence_rows, min_n=10),
        stratification=rm.stratification(
            [_head(head_id="h1", has_research_evidence=True)]
        ),
    )
    finding = rm.build_finding(payload)
    assert "1 research signals" in finding.title
    assert "web_search:search.searxng.local" in finding.body
    assert "has_research_evidence=true: heads=1" in finding.body
    assert "2026-09-04: signals=1" in finding.body
    assert "research_signals_present" in finding.tags
