# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W-6 / W-7 / W-8 — publication, badge and paging (EXTERNAL GRADING AT WIDTH).

Three surfaces, one discipline: a number never leaves this system without the
population it was measured on, the n it rests on, and the state word that says
whether it is a measurement at all. The specific failures pinned here are the
ones the round programme actually produced — a badge that could read 1.00 beside
a fact its own desk denied, an accuracy of 1.00 over two decided claims, and a
verdict paged on one grader's reading of one span on one page.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from legba.data import correctness_axis
from legba.data.analysts.deterministic_handlers import _daily_page_budget as dpb
from legba.data.analysts.deterministic_handlers import alert_trigger_scan as ats
from legba.data.provenance import external_truth as et
from legba.data.provenance import round_lineage as rl
from legba.data.registry.v3_api import UnitCorrectnessBoard, build_v3_router


# ---------------------------------------------------------------------------
# Fixtures — aggregate rows in the shape VERDICT_AGGREGATE_SQL returns
# ---------------------------------------------------------------------------


def _agg(population: str, verdict: str, n: int, **kw: Any) -> dict[str, Any]:
    row = {
        "population": population,
        "verdict": verdict,
        "assembly_regime": "assembly",
        "severity": "high",
        "claim_shape": "positive",
        "desk_tier": "country",
        "source_tier_class": "tier_2" if verdict in ("SUPPORTED", "CONTRADICTED") else None,
        "retrieval_origin": None,
        "n": n,
    }
    row.update(kw)
    return row


RECORD_ROWS = [
    _agg(et.POPULATION_ASSEMBLY_SPAN, "SUPPORTED", 818),
    _agg(et.POPULATION_ASSEMBLY_SPAN, "CONTRADICTED", 330),
    _agg(et.POPULATION_ASSEMBLY_SPAN, "NOT_FOUND", 2142),
    _agg(et.POPULATION_ASSEMBLY_SPAN, "UNCHECKED", 1799),
    _agg(et.POPULATION_ASSEMBLY_SPAN, "UNCHECKABLE", 343),
    _agg(et.POPULATION_ASSESSMENT_SENTENCE, "SUPPORTED", 38, desk_tier="voice"),
    _agg(et.POPULATION_ASSESSMENT_SENTENCE, "CONTRADICTED", 18, desk_tier="voice"),
]


# ---------------------------------------------------------------------------
# W-6 · THE ARITHMETIC
# ---------------------------------------------------------------------------


def test_the_designs_own_worked_example_reproduces() -> None:
    """§3.3's block, verbatim: 1,148 decided of 3,290 searched, rate 0.349."""
    scored = et.score_population_rows(RECORD_ROWS)
    record = scored[et.POPULATION_ASSEMBLY_SPAN]
    assert record["n_decided"] == 1148
    assert record["n_searched"] == 3290
    assert record["n_uncheckable"] == 343
    assert record["n_unchecked"] == 1799
    assert round(record["decided_rate"], 3) == 0.349
    # 818/1148 = 0.71254. The design's block prints 0.712; the arithmetic is the
    # authority and it is carried at four places, so the pin is on the value the
    # code computes rather than on the doc's rounding.
    assert round(record["accuracy"], 4) == 0.7125


def test_not_found_unchecked_and_uncheckable_leave_both_ratios() -> None:
    """The exclusion is exactly ``correctness_axis``' treatment of
    ``unresolvable`` — a label with no weight enters neither the numerator nor
    the denominator, and is reported in its own count instead."""
    only_supported = et.score_verdicts({"SUPPORTED": 40, "CONTRADICTED": 0})
    with_noise = et.score_verdicts(
        {"SUPPORTED": 40, "CONTRADICTED": 0, "NOT_FOUND": 900,
         "UNCHECKED": 900, "UNCHECKABLE": 900}
    )
    assert only_supported["accuracy"] == with_noise["accuracy"] == 1.0
    assert with_noise["n_decided"] == 40
    # ...but the decided RATE moves, which is the honest signal.
    assert only_supported["decided_rate"] == 1.0
    assert round(with_noise["decided_rate"], 3) == 0.043


def test_below_the_floor_the_value_is_none_never_zero() -> None:
    """``0.0`` on a surface reads as a measured failure. A real 0.0 means every
    decided claim was CONTRADICTED, and that must stay distinguishable."""
    thin = et.score_verdicts({"SUPPORTED": 0, "CONTRADICTED": 3})
    assert thin["accuracy"] is None
    assert thin["n_decided"] == 3 and thin["sufficient"] is False
    real_zero = et.score_verdicts({"SUPPORTED": 0, "CONTRADICTED": 40})
    assert real_zero["accuracy"] == 0.0
    assert "below the 10 floor" in et.describe_population(
        {**thin, "population": et.POPULATION_ASSESSMENT_SENTENCE}
    )


def test_the_headline_is_a_pair_and_this_module_cannot_sum_it() -> None:
    """F-12, made structural. The record is the DESKS' sentences quoted verbatim;
    the voice is the Assessment's own. There is deliberately no accessor that
    returns one number over both."""
    scored = et.score_population_rows(RECORD_ROWS)
    pair = et.headline_pair(scored)
    assert set(pair) == {"record", "voice", "note"}
    assert pair["record"]["n_decided"] == 1148
    assert pair["voice"]["n_decided"] == 56
    assert pair["record"]["accuracy"] != pair["voice"]["accuracy"]
    assert "never one" in pair["note"]
    board = et.compose_board({"days": 7, "verdicts": RECORD_ROWS})
    assert not hasattr(board, "accuracy")
    assert "accuracy" not in board.model_dump()
    assert not any(
        callable(getattr(et, name, None)) and "sum" in name
        for name in dir(et)
    )


def test_strata_are_never_pooled_across_populations() -> None:
    """Three populations means three records and no fourth. A stratum cell in one
    population can never absorb a row from another."""
    rows = RECORD_ROWS + [
        _agg(et.POPULATION_LEGACY_PROSE, "SUPPORTED", 11, assembly_regime="legacy"),
        _agg(et.POPULATION_LEGACY_PROSE, "CONTRADICTED", 11, assembly_regime="legacy"),
    ]
    scored = et.score_population_rows(rows)
    assert set(scored) == set(et.POPULATIONS)
    regimes = {
        pop: set(rec["strata"][et.STRATUM_REGIME]) for pop, rec in scored.items()
    }
    assert regimes[et.POPULATION_LEGACY_PROSE] == {"legacy"}
    assert regimes[et.POPULATION_ASSEMBLY_SPAN] == {"assembly"}
    total = sum(rec["n_decided"] for rec in scored.values())
    assert total == 1148 + 56 + 22
    # ...and no single record carries the total.
    assert all(rec["n_decided"] != total for rec in scored.values())


def test_every_named_stratum_is_present_even_when_empty() -> None:
    """``retrieval_origin`` is NULL on all 23,774 signals fetched in 7 days, so
    the stratum ships EMPTY — which is a measurement ("nothing in it"), not an
    omission ("not measured here"). The key exists so the day the research lane's
    write path lands, it fills itself."""
    scored = et.score_population_rows(RECORD_ROWS)
    strata = scored[et.POPULATION_ASSEMBLY_SPAN]["strata"]
    assert set(et.STRATA) <= set(strata)
    assert strata[et.STRATUM_RETRIEVAL_ORIGIN] == {}
    assert set(strata[et.STRATUM_SOURCE_TIER]) == {"tier_2"}


def test_the_visible_tier_unknown_class_is_its_own_stratum_cell() -> None:
    """F-3: the suppression the ruled default would have made silent is instead
    a counted, published class."""
    rows = RECORD_ROWS + [
        _agg(
            et.POPULATION_ASSEMBLY_SPAN, "CONTRADICTED", 12,
            source_tier_class="tier_unknown",
        )
    ]
    scored = et.score_population_rows(rows)
    cells = scored[et.POPULATION_ASSEMBLY_SPAN]["strata"][et.STRATUM_SOURCE_TIER]
    assert cells["tier_unknown"]["contradicted"] == 12
    assert cells["tier_unknown"]["n_decided"] == 12
    assert cells["tier_2"]["n_decided"] == 1148


def test_the_external_axis_is_never_pooled_into_another_aggregate() -> None:
    """The ``labels_api`` P2-5 idiom, applied to the new axis from its first row
    rather than retrofitted after a review finds the number reimplemented."""
    et.assert_not_pooled({"faithfulness": 0.92}, what="a faithfulness aggregate")
    try:
        et.assert_not_pooled(
            {"faithfulness": 0.92, "decided_rate": 0.35},
            what="a faithfulness aggregate",
        )
    except AssertionError as exc:
        assert "decided_rate" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("assert_not_pooled did not raise")
    # And the two judge-independent axes stay segregated from each other too.
    board = et.compose_board({"days": 7, "verdicts": RECORD_ROWS})
    correctness_axis.assert_not_pooled(
        board.model_dump(), what="the external_truth board"
    )


# ---------------------------------------------------------------------------
# W-6 · THE INSTRUMENT (§2.4's stop rule, made continuous)
# ---------------------------------------------------------------------------


def test_the_preregistered_overlap_bands() -> None:
    """R1 0.774, R2 0.804, R3 0.7451 — and R3 died on this exact line."""
    assert et.overlap_band(0.804, 100) == et.BAND_STANDS
    assert et.overlap_band(0.774, 100) == et.BAND_CONTINGENT
    assert et.overlap_band(0.7451, 100) == et.BAND_LIMITED
    # Unmeasured is NOT limited: "not compared yet" and "disagreed" are different
    # facts, and rendering the first as the second lies in the direction that
    # looks rigorous.
    assert et.overlap_band(None, 0) is None
    assert et.overlap_band(0.60, 4) is None


def test_a_limited_instrument_reaches_the_board_as_a_flag_and_a_band() -> None:
    board = et.compose_board({
        "days": 7,
        "verdicts": RECORD_ROWS,
        "conditions": [{
            "population": et.POPULATION_ASSEMBLY_SPAN,
            "grader_family": "gemma", "grader_model_name": "gemma-4-31b",
            "grader_pipeline_version": "2026-09-12/1",
            "search_provider": "searxng", "search_degraded": True,
            "search_liveness": "unverified", "min_sample_fraction": 1.0, "n": 100,
        }],
        "overlap": [{
            "population": et.POPULATION_ASSEMBLY_SPAN,
            "shared": 329, "agree": 245, "audit_families": 1,
        }],
    })
    record = next(
        p for p in board.populations if p.population == et.POPULATION_ASSEMBLY_SPAN
    )
    assert record.instrument["band"] == et.BAND_LIMITED
    assert record.instrument["instrument_limited"] is True
    assert record.instrument["graders"] == ["gemma-4-31b"]
    assert record.instrument["pipeline_version"] == "2026-09-12/1"
    # The search CONDITION is carried, because a week when SearXNG lost eight
    # engines is not a week the world got quieter.
    assert record.search["degraded_share"] == 1.0
    assert record.search["provider_mix"] == {"searxng": 1.0}
    assert record.sampled["sample_fraction"] == 1.0
    assert record.sampled["reason"] is None


def test_a_sampled_day_is_never_published_as_a_whole_one() -> None:
    board = et.compose_board({
        "days": 7,
        "verdicts": RECORD_ROWS,
        "conditions": [{
            "population": et.POPULATION_ASSEMBLY_SPAN, "grader_family": "gemma",
            "grader_model_name": "gemma-4-31b",
            "grader_pipeline_version": "2026-09-12/1",
            "search_provider": "searxng", "search_degraded": False,
            "search_liveness": "verified", "min_sample_fraction": 0.4, "n": 50,
        }],
    })
    record = board.populations[0]
    assert record.sampled["sample_fraction"] == 0.4
    assert "budget exhausted" in record.sampled["reason"]


# ---------------------------------------------------------------------------
# W-6 · THE ROUTE (honest-null before migration 0190 lands)
# ---------------------------------------------------------------------------


class _StubConn:
    """The ``test_v3_eval_correctness`` harness, plus the external_grades reads."""

    def __init__(self, truth_rows: Any = None, raise_on_truth: bool = False) -> None:
        self._truth = truth_rows or {}
        self._raise = raise_on_truth
        self.queries: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> Any:
        self.queries.append(sql)
        if "external_grades" in sql:
            if self._raise:
                raise RuntimeError('relation "external_grades" does not exist')
            if "JOIN public.external_grades" in sql:
                return self._truth.get("overlap", [])
            if "grader_family" in sql:
                return self._truth.get("conditions", [])
            return self._truth.get("verdicts", [])
        return []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.queries.append(sql)
        return None


class _StubDeps:
    def __init__(self, conn: _StubConn) -> None:
        class _Reg:
            pass

        class _Pool:
            def acquire(self_inner) -> Any:  # noqa: N805
                class _Ctx:
                    async def __aenter__(self_ctx) -> _StubConn:  # noqa: N805
                        return conn

                    async def __aexit__(self_ctx, *exc: Any) -> bool:  # noqa: N805
                        return False

                return _Ctx()

        self.descriptor_registry = _Reg()
        self.descriptor_registry.pg = _Pool()  # type: ignore[attr-defined]


def _board(conn: _StubConn) -> UnitCorrectnessBoard:
    router = build_v3_router(deps=_StubDeps(conn))  # type: ignore[arg-type]
    endpoint = next(
        r.endpoint for r in router.routes
        if getattr(r, "path", "") == "/eval/correctness"
    )
    return asyncio.run(endpoint(principal="test"))


def test_the_route_is_honest_null_before_the_ledger_exists() -> None:
    """Migration 0190 is another train's. An ``UndefinedTableError`` is not an
    error condition here — it is "not measured yet", and the block says so."""
    board = _board(_StubConn(raise_on_truth=True))
    assert board.external_truth is not None
    assert board.external_truth.available is False
    assert board.external_truth.populations == []
    assert "never pooled" in board.external_truth.honesty_note


def test_the_route_is_honest_null_with_zero_rows() -> None:
    board = _board(_StubConn(truth_rows={"verdicts": []}))
    assert board.external_truth.available is False
    assert board.external_truth.headline["record"]["accuracy"] is None


def test_the_route_publishes_the_pair_and_the_strata() -> None:
    board = _board(_StubConn(truth_rows={"verdicts": RECORD_ROWS}))
    truth = board.external_truth
    assert truth.available is True
    assert [p.population for p in truth.populations] == [
        et.POPULATION_ASSEMBLY_SPAN, et.POPULATION_ASSESSMENT_SENTENCE
    ]
    assert truth.headline["record"]["n_decided"] == 1148
    assert truth.headline["voice"]["n_decided"] == 56
    record = truth.populations[0]
    assert record.display.startswith("the record")
    assert et.STRATUM_SOURCE_TIER in record.strata
    # The operator axis and the external axis share a ROUTE and share nothing
    # else: two blocks, two contracts, never a pooled number.
    assert board.honesty_note != truth.honesty_note


# ---------------------------------------------------------------------------
# W-7 · THE BADGE
# ---------------------------------------------------------------------------


def test_the_frozen_lineage_is_untouched_by_the_standing_arg() -> None:
    """R4 FREEZE. ``ROUND_LINEAGE`` and its pinning test must not move; the
    standing number JOINS the frozen one, never replaces it."""
    assert [r.round for r in rl.ROUND_LINEAGE] == ["R1", "R2", "R3"]
    scored = et.score_population_rows(RECORD_ROWS)
    badge = rl.assessment_badge(
        standing=et.standing_accuracy(scored[et.POPULATION_ASSEMBLY_SPAN])
    )
    assert badge["external_accuracy"]["value"] == 0.4805
    assert badge["external_accuracy"]["n"] == 77
    assert badge["external_accuracy_note"] == rl.PRE_ASSEMBLY_NOTE
    # ...and the standing block sits BESIDE it.
    assert badge["standing_accuracy"]["value"] == 0.7125
    assert badge["standing_state"] == et.STANDING_MEASURED


def test_the_badge_never_shows_a_bare_standing_number() -> None:
    """Four states, and three of them replace the number with a word. A caller
    that prints ``value`` unconditionally prints an absence, never a rate the
    instrument did not earn."""
    # 1. nothing handed in at all
    badge = rl.assessment_badge()
    assert badge["standing_accuracy"] is None
    assert badge["standing_state"] == rl.STANDING_UNMEASURED

    thin = {"accuracy": None, "n_decided": 3, "n_searched": 3,
            "decided_rate": 1.0, "population": et.POPULATION_ASSESSMENT_SENTENCE,
            "display": "x"}
    # 2. below the floor
    unmeasured = et.standing_accuracy(thin)
    assert unmeasured["value"] is None
    assert unmeasured["state"] == et.STANDING_UNMEASURED
    assert "not graded against the world yet" in unmeasured["state_note"]

    good = {**thin, "accuracy": 0.68, "n_decided": 56}
    # 3. the graders disagreed — the number is WITHHELD, not flagged
    limited = et.standing_accuracy(good, instrument_limited=True)
    assert limited["value"] is None
    assert limited["state"] == et.STANDING_INSTRUMENT_LIMITED
    assert "did not agree with each other" in limited["state_note"]

    # 4. sampled — the rate ships WITH the fraction it covers
    sampled = et.standing_accuracy(good, sample_fraction=0.4)
    assert sampled["value"] == 0.68 and sampled["state"] == et.STANDING_SAMPLED
    assert sampled["sample_fraction"] == 0.4
    assert "sample of the week's claims" in sampled["state_note"]

    # In every state the population and the n travel with the block — a
    # withheld number is still an ANSWER, and an answer without its n is the
    # thing this whole axis exists to stop.
    for block in (unmeasured, limited, sampled):
        assert block["population"] == et.POPULATION_ASSESSMENT_SENTENCE
        assert block["n_decided"] in (3, 56)
        assert block["window_days"] == 7
        assert block["decided_rate"] == 1.0


def test_the_records_standing_note_is_the_demotion_in_one_line() -> None:
    """§3.4: the badge reads 1.00 for CONSTRUCTION and carries the desks' truth
    beside it — the answer, on the page, to *"a badge that can read 1.00 on a
    page containing a fact its own desk denies is not a badge."*"""
    scored = et.score_population_rows(RECORD_ROWS)
    spine = et.record_standing_badge(
        et.standing_accuracy(scored[et.POPULATION_ASSEMBLY_SPAN])
    )
    assert spine["standing_note"] == et.RECORD_STANDING_NOTE
    assert "1.0 by arithmetic" in spine["standing_note"]
    assert "DESK SENTENCES" in spine["standing_note"]
    assert spine["standing_accuracy"]["population"] == et.POPULATION_ASSEMBLY_SPAN
    assert et.record_standing_badge(None) is None


def test_the_producer_emits_the_pair_on_two_keys() -> None:
    """F-12 on the producer side: two keys, so no reader can reach for one."""
    from legba.data.analysts.assessment_channel import build_assessment_payload

    scored = et.score_population_rows(RECORD_ROWS)
    payload = build_assessment_payload(
        payload={"blocks": [], "regime": "assembly"},
        spine_id=str(uuid4()),
        body="The world is quiet.",
        markers=[],
        title_source="model",
        standing=et.standing_accuracy(scored[et.POPULATION_ASSESSMENT_SENTENCE]),
        record_standing=et.standing_accuracy(scored[et.POPULATION_ASSEMBLY_SPAN]),
    )
    voice = payload["badge"]["standing_accuracy"]
    record = payload["spine_badge"]["standing_accuracy"]
    assert voice["population"] == et.POPULATION_ASSESSMENT_SENTENCE
    assert record["population"] == et.POPULATION_ASSEMBLY_SPAN
    assert voice["n_decided"] == 56 and record["n_decided"] == 1148
    # No key anywhere carries a combined number.
    assert "spine_badge" in payload and "badge" in payload
    # Handing in NOTHING leaves the payload byte-shaped as it shipped, minus a
    # spine_badge key that would be a null pretending to be a measurement.
    plain = build_assessment_payload(
        payload={"blocks": [], "regime": "assembly"},
        spine_id=str(uuid4()), body="x", markers=[], title_source="model",
    )
    assert "spine_badge" not in plain
    assert plain["badge"]["standing_state"] == rl.STANDING_UNMEASURED


# ---------------------------------------------------------------------------
# W-8 · THE ALERT PLANE
# ---------------------------------------------------------------------------


def _pageable(**kw: Any) -> dict[str, Any]:
    row = {
        "verdict": "CONTRADICTED",
        "decisive_tier_class": "tier_2",
        "span_resolved": True,
        "claim_severity": "high",
        "audit_verdict": "CONTRADICTED",
    }
    row.update(kw)
    return row


def test_the_class_is_registered_in_every_per_class_registry() -> None:
    """The drift guard: the four places a new trigger class silently half-lands.
    ``verified_finding`` is the ONE documented exception to the unverified-reason
    registry (it carries the finding's real faithfulness score instead)."""
    assert ats.TRIGGER_EXTERNAL_AUDIT == "external_audit"
    assert ats.TRIGGER_EXTERNAL_AUDIT in ats.TRIGGER_CLASSES
    for cls in ats.TRIGGER_CLASSES:
        assert cls in ats._CLASS_PRIORITY, cls
        if cls != ats.TRIGGER_FINDING:
            assert cls in ats._UNVERIFIED_REASONS, cls
    assert len(set(ats._CLASS_PRIORITY.values())) == len(ats._CLASS_PRIORITY)
    assert ats._CHANNEL_BY_CLASS[ats.TRIGGER_EXTERNAL_AUDIT] == "external_audit"
    # And the string is the SAME one the auditor already stamps on its rows and
    # its watermark partition — a vocabulary registration, not a rename.
    from legba.data.analysts.deterministic_handlers import standing_auditor as sa

    assert sa.ALERT_TRIGGER_CLASS == ats.TRIGGER_EXTERNAL_AUDIT


def test_all_five_paging_preconditions_are_required() -> None:
    """Each one failed somewhere in the round record; each one alone withholds."""
    assert ats.external_audit_pages(_pageable()) is True
    breakers = {
        "verdict_contradicted": {"verdict": "SUPPORTED"},
        "tier_1_or_2": {"decisive_tier_class": "tier_unknown"},
        "span_resolved": {"span_resolved": False},
        "severity_high_or_critical": {"claim_severity": "low"},
        "audit_rater_confirmed": {"audit_verdict": "NOT_FOUND"},
    }
    assert set(breakers) == set(
        ats._external_audit_paging.PAGING_PRECONDITIONS
    )
    for name, override in breakers.items():
        decision = ats.external_audit_page_decision(_pageable(**override))
        assert decision.pages is False, name
        assert decision.failed == (name,), name
        assert name in decision.reason


def test_a_single_rater_contradicted_writes_a_row_and_does_not_page() -> None:
    """§3.5.7 — *"A CONTRADICTED verdict is one grader's reading of one span on
    one page. Two families must agree before it pages."* A missing
    ``audit_verdict`` means no second family looked; that is the designed
    outcome, not a degradation."""
    single = _pageable()
    single.pop("audit_verdict")
    decision = ats.external_audit_page_decision(single)
    assert decision.pages is False
    assert decision.failed == ("audit_rater_confirmed",)
    # Every OTHER precondition held — the verdict is real, it is in the ledger,
    # it is in the published number. It simply does not wake anyone.
    assert len(decision.passed) == 4


def test_tier_unknown_is_published_but_never_pages() -> None:
    """F-3's visible class is a PUBLICATION decision, not a paging one:
    publishing a number nobody is woken by is a different act from waking
    somebody at 3am on an unregistered domain."""
    decision = ats.external_audit_page_decision(
        _pageable(decisive_tier_class="tier_unknown")
    )
    assert decision.pages is False
    assert decision.failed == ("tier_1_or_2",)


def test_the_daily_budget_caps_external_audit_at_two() -> None:
    """2/day inside the existing 5/day fleet budget. The generic per-kind cap is
    3; this class declares a tighter one and the override may only ever LOWER."""
    assert ats.EXTERNAL_AUDIT_PAGE_CAP == 2
    assert dpb.cap_for_kind(ats.TRIGGER_EXTERNAL_AUDIT, 3) == 2
    assert dpb.cap_for_kind(ats.TRIGGER_EXTERNAL_AUDIT, 99) == 2
    assert dpb.cap_for_kind(ats.TRIGGER_BAND, 3) == 3
    # ...and a fleet cap tighter than the override still wins.
    assert dpb.cap_for_kind(ats.TRIGGER_EXTERNAL_AUDIT, 1) == 1

    cands = [
        ats.AlertCandidate(
            trigger_class=ats.TRIGGER_EXTERNAL_AUDIT,
            target_id="tr", severity="critical",
            title=f"External audit CONTRADICTED a critical claim #{i}",
            body="", event_at=None, effective_confidence=1.0,
            data={}, watermarks=[],
        )
        for i in range(5)
    ]
    deferred = ats.apply_daily_page_budget(
        cands, already_paged_today=0, budget=5,
        per_kind_cap=dpb.DEFAULT_BUDGET_PER_KIND_CAP,
    )
    taken = [c for c in cands if c.data["budget_deferred"] is False]
    assert len(taken) == 2, "the per-class cap binds before the fleet budget"
    assert deferred == 3


def test_the_cap_leaves_the_rest_of_the_fleet_budget_for_other_classes() -> None:
    """The kind-diversity point: a slot a capped class cannot take goes to a
    LOWER-ranked candidate of a different kind rather than being burned."""
    external = [
        ats.AlertCandidate(
            trigger_class=ats.TRIGGER_EXTERNAL_AUDIT, target_id="tr",
            severity="critical", title=f"ext {i}", body="", event_at=None,
            effective_confidence=1.0, data={}, watermarks=[],
        )
        for i in range(4)
    ]
    others = [
        ats.AlertCandidate(
            trigger_class=ats.TRIGGER_BAND, target_id="us", severity="high",
            title=f"band {i}", body="", event_at=None,
            effective_confidence=1.0, data={}, watermarks=[],
        )
        for i in range(3)
    ]
    ats.apply_daily_page_budget(
        external + others, already_paged_today=0, budget=5,
        per_kind_cap=dpb.DEFAULT_BUDGET_PER_KIND_CAP,
    )
    took = {
        c.trigger_class
        for c in external + others
        if c.data["budget_deferred"] is False
    }
    assert took == {ats.TRIGGER_EXTERNAL_AUDIT, ats.TRIGGER_BAND}
    assert sum(
        1 for c in external if c.data["budget_deferred"] is False
    ) == 2
    assert sum(1 for c in others if c.data["budget_deferred"] is False) == 3
