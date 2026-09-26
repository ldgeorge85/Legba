# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 6 L2 — the ``layer_divergence`` divergence-baseline unit.

Covers, in order:

  * the WIRE FOLD — content_hash, normalized headline, the declared-wire
    relaxation, the O(n^2) bound, and the placement rule (folding is per
    (layer, day) and NEVER across layers, because one dispatch running in both
    the domestic and the foreign press is the narrative-control pair's subject
    rather than a duplicate);
  * the BASELINE ARITHMETIC on a hand-computed fixture series — median, MAD,
    the 1.4826 scale, the MAD floor, the blank-day exclusion and the
    ``_BASELINE_MIN_DAYS`` floor;
  * the TWO-DAY RULE — one day over threshold does not fire, two do, a sign
    flip across the two does not, and the direction label;
  * the APERTURE EXCLUSIONS — absent / unmeasured / undeclared each excluded,
    each NAMED, absent contributing no count, and a pair skipped when either
    side is excluded;
  * the THIN FLAG — set from the counts, never suppressing the finding;
  * the RECEIPT — per-layer daily counts, the baselines, the pairs, and WHY
    nothing fired on a quiet run;
  * the wiring — SUB_HANDLERS, OUTPUT_KIND, both verify registries, the option
    catalog, METHOD_VERSION's §10.9 row, and the SEAMS #60 stamp;
  * the LIVE path against a real Postgres (migration 0214): a seeded map +
    seeded signals driven through ``deterministic.run_method`` — the REAL
    binding path, not the handler function — reading back what the four
    queries actually return.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    run_method,
)
from legba.data.analysts.deterministic_handlers import layer_divergence as ld
from legba.data.analysts.deterministic_handlers import _layer_fold as lf
from legba.data.analysts.handler_options import HANDLER_OPTIONS, known_option_names
from legba.data.config import PostgresConfig
from legba.data.layers._vocab import LAYER_VOCAB
from legba.data.provenance.kinds import (
    STRUCTURAL_CLAIMS_VERIFY_ANALYSTS,
    STRUCTURAL_VERIFY_EXEMPT_ANALYSTS,
    OutputKind,
)
from legba.data.provenance.verify import verify_structural_claims
from legba.runtime.analyst_method import AnalystMethodResult

REPO_ROOT = Path(__file__).resolve().parents[2]
AS_OF = date(2026, 9, 24)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _item(
    source_id: str,
    day: str,
    n: int = 0,
    *,
    title: str = "",
    content_hash: str = "",
) -> lf.SignalItem:
    return lf.SignalItem(
        signal_id=str(uuid4()),
        source_id=source_id,
        ts=datetime.fromisoformat(f"{day}T12:00:00+00:00") - timedelta(minutes=n),
        content_hash=content_hash,
        title=title or f"a perfectly ordinary headline number {n}",
    )


def _apertures(present: tuple[str, ...], **overrides: tuple[str, str]) -> list[dict]:
    """All six layers declared: named ones ``present``, rest ``unmeasured``."""
    rows = []
    for layer in LAYER_VOCAB:
        if layer in overrides:
            declared, reason = overrides[layer]
        elif layer in present:
            declared, reason = "present", ""
        else:
            declared, reason = "unmeasured", ""
        rows.append({"layer": layer, "declared": declared, "reason": reason})
    return rows


def _day(offset: int) -> str:
    """``offset`` days before :data:`AS_OF`, as an ISO day key."""
    return (AS_OF - timedelta(days=offset)).isoformat()


def _steady_bundle(
    *,
    official_by_day: dict[str, int] | None = None,
    social_per_day: int = 10,
    official_per_day: int = 10,
    window: int = 28,
    apertures: list[dict] | None = None,
    layer_map: dict[str, str] | None = None,
) -> dict:
    """A desk with two present layers at steady volume, overridable per day."""
    signals: list[lf.SignalItem] = []
    for i in range(window):
        day = _day(window - 1 - i)
        n_off = (official_by_day or {}).get(day, official_per_day)
        for k in range(n_off):
            signals.append(_item("src.off", day, k, title=f"official {k} {day}"))
        for k in range(social_per_day):
            signals.append(_item("src.soc", day, k, title=f"social {k} {day}"))
    return {
        "target_id": "country_watch_ru",
        "country": "RU",
        "map_version": "layer_map_ru.v1",
        "layer_map": layer_map or {"src.off": "official", "src.soc": "social_digest"},
        "apertures": apertures or _apertures(("official", "social_digest")),
        "signals": signals,
    }


async def _run(bundles: list[dict], **options: Any) -> AnalystMethodResult:
    opts = {"as_of": AS_OF.isoformat(), "analyst_id": "layer_divergence", **options}
    return await ld.handle(bundles, opts, None)


def _pair(result: AnalystMethodResult, pair_id: str) -> dict:
    (target,) = result.finding.data["targets"]
    return next(p for p in target["pairs"] if p["pair_id"] == pair_id)


# ---------------------------------------------------------------------------
# 1. The fold
# ---------------------------------------------------------------------------


def test_fold_collapses_identical_content_hashes():
    items = [
        _item("source.a", _day(0), 1, content_hash="abc", title="one headline here"),
        _item("source.b", _day(0), 2, content_hash="abc", title="quite another thing"),
        _item("source.c", _day(0), 3, content_hash="zzz", title="a third distinct one"),
    ]
    kept, folded = lf.fold_bucket(items, max_fold_rows=60)
    assert (len(kept), folded) == (2, 1)


def test_fold_collapses_identical_normalized_headlines_across_outlets():
    """``HEADLINE_MAX_DISTANCE`` is 0.0 for an undeclared pair — identical
    normalized headlines from two outlets on one day is syndication."""
    title = "Russia announces new export restrictions on fertiliser"
    items = [
        _item("source.x", _day(0), 1, title=title),
        _item("source.y", _day(0), 2, title=f"  {title.upper()}!  "),
    ]
    kept, folded = lf.fold_bucket(items, max_fold_rows=60)
    assert (len(kept), folded) == (1, 1)


def test_fold_keeps_short_headlines_apart():
    """A headline under ``_MIN_KEY_TOKENS`` is not an identity — a degenerate
    key folds everything it touches."""
    items = [
        _item("source.x", _day(0), 1, title="Morning briefing"),
        _item("source.y", _day(0), 2, title="Morning briefing"),
    ]
    kept, folded = lf.fold_bucket(items, max_fold_rows=60)
    assert (len(kept), folded) == (2, 0)


def test_fold_uses_the_declared_bar_only_for_a_declared_pair():
    """Two REAL declared Reuters re-carriers fold on a trimmed headline; two
    unrelated outlets running the same trimmed pair do not."""
    long_title = "Israeli cabinet approves the revised budget framework for winter"
    trimmed = "Israeli cabinet approves revised budget framework for winter"

    declared = [
        _item("source.cna.all", _day(0), 1, title=long_title),
        _item("source.jpost.frontpage", _day(0), 2, title=trimmed),
    ]
    kept_d, folded_d = lf.fold_bucket(declared, max_fold_rows=60)
    assert (len(kept_d), folded_d) == (1, 1)

    undeclared = [
        _item("source.unrelated.one", _day(0), 1, title=long_title),
        _item("source.unrelated.two", _day(0), 2, title=trimmed),
    ]
    kept_u, folded_u = lf.fold_bucket(undeclared, max_fold_rows=60)
    assert (len(kept_u), folded_u) == (2, 0)


def test_fold_folds_two_feeds_of_one_publisher_on_a_trimmed_headline():
    """``SAME_PUBLISHER`` relaxes the bar the same way ``SYNDICATION`` does."""
    items = [
        _item(
            "source.un_news.africa", _day(0), 1,
            title="UN agency warns of worsening food insecurity in the Sahel",
        ),
        _item(
            "source.un_news.middle_east", _day(0), 2,
            title="UN agency warns of worsening food insecurity in Sahel",
        ),
    ]
    kept, folded = lf.fold_bucket(items, max_fold_rows=60)
    assert (len(kept), folded) == (1, 1)


def test_fold_walk_bound_over_counts_rather_than_under_counts():
    """Rows past ``max_fold_rows`` still COUNT — the safe direction."""
    title = "Israeli cabinet approves the revised budget framework for winter"
    trimmed = "Israeli cabinet approves revised budget framework for winter"
    items = [
        _item("source.cna.all", _day(0), 1, title=title),
        _item("source.jpost.frontpage", _day(0), 2, title=trimmed),
    ]
    kept, folded = lf.fold_bucket(items, max_fold_rows=1)
    assert (len(kept), folded) == (2, 0)


def test_fold_representative_is_the_newest_and_is_stable():
    title = "Russia announces new export restrictions on fertiliser"
    newest = _item("source.x", _day(0), 0, title=title)
    older = _item("source.y", _day(0), 90, title=title)
    for order in ([newest, older], [older, newest]):
        kept, _ = lf.fold_bucket(order, max_fold_rows=60)
        assert [k.signal_id for k in kept] == [newest.signal_id]


def test_the_same_dispatch_in_two_layers_counts_once_in_each():
    """THE PLACEMENT RULE. Folding is per (layer, day). A wire story running
    in the domestic AND the foreign press is the narrative-control pair's
    SUBJECT, not a duplicate — a global fold would delete the measurement."""
    title = "Russia announces new export restrictions on fertiliser"
    bundle = lf.TargetBundle(
        target_id="t", country="RU",
        layer_map={"src.dom": "domestic_press", "src.for": "foreign_press"},
        signals=[
            _item("src.dom", _day(0), 1, title=title),
            _item("src.for", _day(0), 2, title=title),
        ],
    )
    counts, _reps = lf.count_layer_days(
        bundle, days=[_day(0)], max_fold_rows=60
    )
    assert counts["domestic_press"][_day(0)].kept == 1
    assert counts["foreign_press"][_day(0)].kept == 1


def test_a_source_outside_the_map_is_counted_in_no_layer():
    bundle = lf.TargetBundle(
        target_id="t", country="RU",
        layer_map={"src.dom": "domestic_press"},
        signals=[
            _item("src.dom", _day(0), 1),
            _item("src.unmapped", _day(0), 2),
        ],
    )
    counts, _ = lf.count_layer_days(bundle, days=[_day(0)], max_fold_rows=60)
    assert counts["domestic_press"][_day(0)].kept == 1
    assert set(counts) == {"domestic_press"}


# ---------------------------------------------------------------------------
# 2. The baseline arithmetic, on a fixture series
# ---------------------------------------------------------------------------


def test_log_ratio_is_symmetric_and_defined_at_zero():
    assert ld.log_ratio(10, 10) == pytest.approx(0.0)
    assert ld.log_ratio(3, 7) == pytest.approx(-ld.log_ratio(7, 3))
    # A silent layer is a finite number, not an exclusion.
    assert ld.log_ratio(0, 12) == pytest.approx(-4.643856, abs=1e-5)


def test_median_and_mad_on_a_hand_computed_series():
    values = [1.0, 2.0, 3.0, 4.0, 100.0]
    assert ld.median(values) == 3.0
    # |1-3|,|2-3|,|3-3|,|4-3|,|100-3| -> [2,1,0,1,97] -> median 1
    assert ld.mad(values, 3.0) == 1.0
    assert ld.median([1.0, 2.0, 3.0, 4.0]) == 2.5


def test_baseline_scales_mad_by_the_normal_consistency_constant():
    values = [0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0]
    base = ld.baseline_of(values, mad_floor=0.0)
    assert base.n == 7
    assert base.centre == 1.0
    assert base.raw_mad == 0.0
    # MAD 0 with a zero floor leaves a zero scale — reported, never guessed.
    assert base.scale == 0.0
    assert base.floored is False


def test_baseline_floor_applies_and_is_reported():
    base = ld.baseline_of([2.0] * 10, mad_floor=0.2)
    assert base.raw_mad == 0.0
    assert base.scale == 0.2
    assert base.floored is True
    assert base.to_dict()["scale_floored"] is True


def test_baseline_refuses_below_the_minimum_day_count():
    thin = ld.baseline_of([1.0] * (ld._BASELINE_MIN_DAYS - 1), mad_floor=0.2)
    assert thin.usable is False
    assert thin.centre is None
    ok = ld.baseline_of([1.0] * ld._BASELINE_MIN_DAYS, mad_floor=0.2)
    assert ok.usable is True


def test_z_is_computed_against_the_days_before_and_not_the_day_itself():
    """A hand-computed series: 14 days at ratio 0 (10 vs 10), then one day at
    0 vs 10. The last day's baseline must be the FLAT prior fortnight."""
    counts = {
        "official": {_day(i): lf.DayCount(kept=0 if i == 0 else 10) for i in range(15)},
        "social_digest": {_day(i): lf.DayCount(kept=10) for i in range(15)},
    }
    days = [_day(14 - i) for i in range(15)]
    series, _div, _reason = ld.evaluate_pair(
        ld.PAIRS_OF_INTEREST[0], counts,
        days=days, baseline_days=14, z_threshold=2.0, mad_floor=0.2,
    )
    last = series[-1]
    assert last.a == 0 and last.b == 10
    assert last.baseline.n == 14
    assert last.baseline.centre == pytest.approx(0.0)
    # (log2(0.5/10.5) - 0) / 0.2  — the floored scale.
    assert last.z == pytest.approx(ld.log_ratio(0, 10) / 0.2, abs=1e-6)


def test_a_blank_day_neither_sets_the_baseline_nor_is_tested():
    """Both layers silent is a quiet Sunday or a stalled poll — not evidence
    about the country (the weekend-dip lesson)."""
    counts = {
        "official": {_day(i): lf.DayCount(kept=0 if i < 2 else 10) for i in range(15)},
        "social_digest": {
            _day(i): lf.DayCount(kept=0 if i < 2 else 10) for i in range(15)
        },
    }
    days = [_day(14 - i) for i in range(15)]
    series, divergence, reason = ld.evaluate_pair(
        ld.PAIRS_OF_INTEREST[0], counts,
        days=days, baseline_days=14, z_threshold=2.0, mad_floor=0.2,
    )
    assert series[-1].blank is True
    assert series[-1].z is None
    assert divergence is None
    assert reason == "blank_day"


# ---------------------------------------------------------------------------
# 3. The two-day rule
# ---------------------------------------------------------------------------


async def test_one_day_over_threshold_does_not_fire():
    result = await _run([_steady_bundle(official_by_day={_day(0): 0})])
    assert result.finding.data["divergence_count"] == 0
    assert result.force_trace_only is True
    assert _pair(result, "regime_public_gap")["no_fire_reason"] == "below_threshold"


async def test_two_consecutive_days_over_threshold_fire():
    result = await _run(
        [_steady_bundle(official_by_day={_day(0): 0, _day(1): 0})]
    )
    data = result.finding.data
    assert data["divergence_count"] == 1
    assert result.force_trace_only is False
    (div,) = data["divergences"]
    assert div["pair_id"] == "regime_public_gap"
    assert div["consecutive_days"] == 2
    assert div["louder_layer"] == "social_digest"
    assert div["direction"] == "widening"
    assert len(div["z_prior_days"]) == 1


async def test_a_sign_flip_across_the_two_days_does_not_fire():
    """Day -1 the official layer floods, day 0 it goes silent. Both days clear
    the threshold; the move is not a direction, so nothing fires."""
    result = await _run(
        [_steady_bundle(official_by_day={_day(1): 400, _day(0): 0})]
    )
    assert result.finding.data["divergence_count"] == 0
    assert _pair(result, "regime_public_gap")["no_fire_reason"] == "sign_flipped"


async def test_narrowing_is_named_when_the_gap_closes_toward_parity():
    """Baseline is a WIDE standing gap (1 vs 10); the last two days close it."""
    result = await _run([
        _steady_bundle(
            official_by_day={
                **{_day(i): 1 for i in range(2, 28)},
                _day(1): 10,
                _day(0): 10,
            },
        )
    ])
    (div,) = result.finding.data["divergences"]
    assert div["direction"] == "narrowing"


async def test_consecutive_days_is_a_constant_not_a_knob():
    """The two-day rule IS the measure — no option can set it to 1."""
    assert ld._CONSECUTIVE_DAYS == 2
    assert "consecutive_days" not in set(known_option_names("layer_divergence"))
    src = Path(ld.__file__).read_text(encoding="utf-8")
    assert 'options.get("consecutive_days"' not in src


async def test_severity_and_confidence_come_from_the_z_and_the_counts():
    assert ld._severity_for(2.1) == "moderate"
    assert ld._severity_for(4.0) == "elevated"
    assert ld._severity_for(9.9) == "high"
    # The ladder deliberately never reaches `critical`.
    assert "critical" not in {ld._severity_for(z) for z in (2.0, 5.0, 500.0)}
    assert ld._confidence_for(40, 40, full_n=20) == 1.0
    assert ld._confidence_for(10, 40, full_n=20) == 0.5
    assert ld._confidence_for(0, 40, full_n=20) == ld._MIN_CONF


# ---------------------------------------------------------------------------
# 4. The aperture exclusions
# ---------------------------------------------------------------------------


def test_aperture_view_places_every_layer_in_exactly_one_bucket():
    view = ld.aperture_view({
        "official": ("present", ""),
        "domestic_press": ("absent", "no independent press operates"),
        "foreign_press": ("unmeasured", ""),
        # social_digest / public_data / physical have NO row -> undeclared
    })
    assert view.present == ["official"]
    rendered = view.to_dict()
    assert rendered["present"] == ["official"]
    assert rendered["absent"] == [
        {"layer": "domestic_press", "reason": "no independent press operates"}
    ]
    assert [r["layer"] for r in rendered["unmeasured"]] == ["foreign_press"]
    assert [r["layer"] for r in rendered["undeclared"]] == [
        "physical", "public_data", "social_digest"
    ]
    placed = (
        len(rendered["present"]) + len(rendered["absent"])
        + len(rendered["unmeasured"]) + len(rendered["undeclared"])
    )
    assert placed == len(LAYER_VOCAB)


async def test_an_absent_layer_contributes_no_count_and_the_receipt_says_so():
    """An `absent` layer that the MAP nevertheless carries sources for: the
    count is suppressed AND the disagreement is reported, never hidden."""
    bundle = _steady_bundle(
        apertures=_apertures(
            ("social_digest",),
            official=("absent", "the ministry publishes nothing online"),
        ),
    )
    result = await _run([bundle])
    (target,) = result.finding.data["targets"]
    assert target["counts_suppressed_by_aperture"] == {"official": 28 * 10}
    assert all(
        d["kept"] == 0 for d in target["layers"]["official"]["daily"].values()
    )
    assert target["aperture"]["absent"] == [
        {"layer": "official", "reason": "the ministry publishes nothing online"}
    ]


async def test_a_pair_is_skipped_and_named_when_either_side_is_excluded():
    bundle = _steady_bundle(
        apertures=_apertures(
            ("social_digest",),
            official=("absent", "the ministry publishes nothing online"),
        ),
    )
    result = await _run([bundle])
    row = _pair(result, "regime_public_gap")
    assert row["evaluable"] is False
    assert row["no_fire_reason"] == "aperture_excluded"
    assert row["excluded_layers"] == [{
        "layer": "official",
        "state": "absent",
        "reason": "the ministry publishes nothing online",
    }]


async def test_unmeasured_and_undeclared_are_excluded_and_distinguished():
    """docs/LAYERS.md is emphatic: an undeclared layer is NOT unmeasured."""
    partial = [
        {"layer": "official", "declared": "present", "reason": ""},
        {"layer": "social_digest", "declared": "unmeasured", "reason": ""},
    ]
    result = await _run([_steady_bundle(apertures=partial)])
    (target,) = result.finding.data["targets"]
    assert [r["layer"] for r in target["aperture"]["unmeasured"]] == [
        "social_digest"
    ]
    assert {r["layer"] for r in target["aperture"]["undeclared"]} == {
        "domestic_press", "foreign_press", "public_data", "physical"
    }
    assert _pair(result, "regime_public_gap")["no_fire_reason"] == "aperture_excluded"


# ---------------------------------------------------------------------------
# 5. The thin flag
# ---------------------------------------------------------------------------


async def test_thin_is_flagged_and_never_suppresses_the_finding():
    """A regime going silent IS a thin official layer. Hiding the finding
    because the layer is thin would delete the measurement."""
    result = await _run(
        [_steady_bundle(official_by_day={_day(0): 0, _day(1): 0})]
    )
    (div,) = result.finding.data["divergences"]
    assert div["thin"] is True
    assert div["thin_layers"] == ["official"]
    assert div["thin_min_per_day"] == 5
    assert "thin_layer" in result.finding.tags
    assert "[thin]" in result.finding.body
    assert "THIN:" in result.finding.body


async def test_a_well_fed_pair_is_not_thin():
    result = await _run([
        _steady_bundle(
            social_per_day=10,
            official_by_day={_day(0): 60, _day(1): 60},
        )
    ])
    (div,) = result.finding.data["divergences"]
    assert div["thin"] is False
    assert div["thin_layers"] == []
    assert "thin_layer" not in result.finding.tags


async def test_thin_min_per_day_zero_marks_nothing_thin():
    result = await _run(
        [_steady_bundle(official_by_day={_day(0): 0, _day(1): 0})],
        thin_min_per_day=0,
    )
    (div,) = result.finding.data["divergences"]
    assert div["thin"] is False


# ---------------------------------------------------------------------------
# 6. The receipt
# ---------------------------------------------------------------------------


async def test_receipt_carries_the_per_layer_daily_counts():
    result = await _run([_steady_bundle()])
    (target,) = result.finding.data["targets"]
    daily = target["layers"]["official"]["daily"]
    assert len(daily) == 28
    assert daily[_day(0)] == {"kept": 10, "folded": 0, "raw": 10}
    assert target["layers"]["official"]["sources_mapped"] == 1
    assert target["country"] == "RU"
    assert target["map_version"] == "layer_map_ru.v1"


async def test_receipt_carries_the_baselines_and_the_series():
    result = await _run([_steady_bundle()])
    row = _pair(result, "regime_public_gap")
    assert row["evaluable"] is True
    assert len(row["series"]) == 28
    last = row["series"][-1]
    assert set(last) == {"day", "a", "b", "log_ratio", "blank", "z", "baseline"}
    assert set(last["baseline"]) == {"n", "centre", "mad", "scale", "scale_floored"}
    assert last["baseline"]["n"] == 14


async def test_receipt_names_why_no_finding_fired_on_a_quiet_run():
    result = await _run([_steady_bundle()])
    data = result.finding.data
    assert data["divergence_count"] == 0
    assert result.force_trace_only is True
    assert _pair(result, "regime_public_gap")["no_fire_reason"] == "below_threshold"
    # The unevaluable pairs name the aperture, not a fake threshold verdict.
    assert _pair(result, "narrative_control")["no_fire_reason"] == "aperture_excluded"
    assert "below_threshold" in result.finding.body
    assert "aperture_excluded" in result.finding.body


async def test_receipt_carries_the_run_parameters_and_the_versions():
    result = await _run([_steady_bundle()], window_days=21, baseline_days=10)
    data = result.finding.data
    assert data["method_version"] == ld.METHOD_VERSION
    assert data["payload_schema"] == ld.LAYER_DIVERGENCE_VERSION
    assert data["as_of"] == AS_OF.isoformat()
    assert data["window_days"] == 21
    assert data["baseline_days"] == 10
    assert data["consecutive_days"] == 2
    assert data["z_threshold"] == 2.0
    assert data["mad_floor"] == 0.2


async def test_receipt_stamps_the_classification_audit_seam_verbatim():
    """SEAMS #60 — no reader may mistake an un-audited map for an audited
    one, on a firing run or a quiet one."""
    quiet = await _run([_steady_bundle()])
    fired = await _run(
        [_steady_bundle(official_by_day={_day(0): 0, _day(1): 0})]
    )
    for result in (quiet, fired):
        note = result.finding.data["classification_audit"]
        assert note == ld.CLASSIFICATION_AUDIT_NOTE
        assert "SEAMS #60" in note


async def test_unresolved_and_malformed_targets_are_named_not_dropped():
    result = await _run([_steady_bundle()], divergence_targets=["not a target!"])
    assert any(
        "malformed" in w for w in result.finding.data["warnings"]
    )


async def test_baseline_wider_than_the_window_is_warned():
    result = await _run([_steady_bundle()], window_days=10, baseline_days=14)
    assert any(
        "no day in the window has a full baseline" in w
        for w in result.finding.data["warnings"]
    )


# ---------------------------------------------------------------------------
# 7. Citations, lineage, structural claims, determinism
# ---------------------------------------------------------------------------


async def test_citations_are_real_fold_representatives_with_contiguous_ordinals():
    bundle = _steady_bundle(official_by_day={_day(0): 3, _day(1): 3})
    result = await _run([bundle], max_citations_per_layer=2)
    cits = result.finding.data["citations"]
    assert cits, "a fired divergence must cite the day's signals"
    assert [c["ordinal"] for c in cits] == list(range(1, len(cits) + 1))
    assert [c["marker"] for c in cits] == [f"[{i}]" for i in range(1, len(cits) + 1)]
    assert {c["layer"] for c in cits} == {"official", "social_digest"}
    assert all(c["day"] == _day(0) for c in cits)
    real_ids = {s.signal_id for s in bundle["signals"]}
    assert all(c["signal_id"] in real_ids for c in cits)
    # At most `max_citations_per_layer` per layer.
    for layer in ("official", "social_digest"):
        assert sum(1 for c in cits if c["layer"] == layer) <= 2


async def test_derived_from_names_the_cited_signals_and_dedupes():
    result = await _run(
        [_steady_bundle(official_by_day={_day(0): 0, _day(1): 0})]
    )
    cited = {c["signal_id"] for c in result.finding.data["citations"]}
    assert {str(u) for u in result.derived_from} == cited
    assert all(isinstance(u, UUID) for u in result.derived_from)
    assert len(set(result.derived_from)) == len(result.derived_from)


async def test_structural_claims_re_derive_as_supported():
    result = await _run(
        [_steady_bundle(official_by_day={_day(0): 0, _day(1): 0})]
    )
    report = verify_structural_claims(
        data=result.finding.data,
        derived_from=[str(u) for u in result.derived_from],
    )
    assert report.had_claims is True
    assert report.claim_verdicts, "the claims block must be present"
    assert report.supported == len(report.claim_verdicts), [
        (v.claim_id, v.verdict, v.detail) for v in report.claim_verdicts
    ]
    assert report.miscount == 0


async def test_the_run_is_a_pure_function_of_its_window():
    """A replay over an unchanged window reproduces the body byte for byte —
    which is what makes the proof round re-derivable rather than remembered."""
    bundle = _steady_bundle(official_by_day={_day(0): 0, _day(1): 0})
    first = await _run([bundle])
    second = await _run([bundle])
    assert first.finding.body == second.finding.body
    assert first.finding.title == second.finding.title


async def test_zero_token_usage_always():
    result = await _run([_steady_bundle()])
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0
    }


async def test_empty_inputs_are_trace_only_and_honest():
    result = await _run([])
    assert result.finding.data["divergence_count"] == 0
    assert result.finding.data["targets_evaluated"] == 0
    assert result.force_trace_only is True
    assert "no gap change" in result.finding.title.lower()


# ---------------------------------------------------------------------------
# 8. Wiring
# ---------------------------------------------------------------------------


def test_registered_as_a_finding_emitting_sub_handler():
    assert SUB_HANDLERS["layer_divergence"] is ld.handle
    assert OUTPUT_KIND_BY_SUB_HANDLER["layer_divergence"] is OutputKind.FINDING


def test_registered_in_both_verify_registries():
    assert "layer_divergence" in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
    assert "layer_divergence" in STRUCTURAL_CLAIMS_VERIFY_ANALYSTS
    assert STRUCTURAL_CLAIMS_VERIFY_ANALYSTS <= STRUCTURAL_VERIFY_EXEMPT_ANALYSTS


def test_every_declared_knob_is_read_and_every_read_knob_is_declared():
    declared = set(known_option_names("layer_divergence"))
    assert declared, "the catalog entry must not be empty"
    src = Path(ld.__file__).read_text(encoding="utf-8")
    for name in declared:
        assert f'options.get("{name}"' in src, name
    assert HANDLER_OPTIONS["layer_divergence"]


def test_day_basis_is_choice_locked_because_it_reaches_sql():
    (spec,) = [
        s for s in HANDLER_OPTIONS["layer_divergence"] if s.name == "day_basis"
    ]
    assert spec.choices == ("fetched_at", "created_at")
    ok, _value, _cause = spec.validate("fetched_at; DROP TABLE signals")
    assert ok is False


def test_method_version_has_its_docs_row():
    section = (
        (REPO_ROOT / "docs" / "ANALYSIS.md")
        .read_text(encoding="utf-8")
        .split("### 10.9 Method versions", 1)[1]
        .split("### ", 1)[0]
    )
    assert f"`{ld.METHOD_VERSION}`" in section
    assert "| `layer_divergence` |" in section


def test_the_seam_is_declared_in_the_registry():
    seams = (REPO_ROOT / "docs" / "SEAMS.md").read_text(encoding="utf-8")
    assert "| 60 | Sampled layer-classification audit |" in seams
    assert "CLASSIFICATION_AUDIT_NOTE" in seams


def test_the_descriptor_exists_and_is_a_draft_deterministic_analyst():
    import yaml

    from legba.data.schemas.analyst import AnalystDescriptor

    path = REPO_ROOT / "descriptors" / "analyst_layer_divergence.yaml"
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    # The version placeholder is stamped at register time (the house
    # convention every analyst descriptor in the tree uses).
    body["identity"]["version"] = "a" * 16
    descriptor = AnalystDescriptor.model_validate(body, strict=False)
    assert descriptor.identity.id == ld.SUB_HANDLER_NAME
    assert descriptor.method.sub_handler == ld.SUB_HANDLER_NAME
    assert descriptor.identity.state.value == "draft"
    assert descriptor.method.kind == "deterministic"
    # Deterministic: no LLM block, and therefore no token budget to declare.
    assert not descriptor.method.llm
    raw_text = path.read_text(encoding="utf-8")
    assert "max_tokens" not in raw_text
    assert "budget_tokens_per_day" not in raw_text
    assert descriptor.subscription.targets is None  # META: one global run


def test_the_descriptor_options_all_resolve_with_no_rejects():
    import yaml

    from legba.data.analysts.handler_options import resolve_handler_options
    from legba.data.schemas.analyst import AnalystDescriptor

    path = REPO_ROOT / "descriptors" / "analyst_layer_divergence.yaml"
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    body["identity"]["version"] = "a" * 16
    descriptor = AnalystDescriptor.model_validate(body, strict=False)
    resolution = resolve_handler_options(
        "layer_divergence", dict(descriptor.method.options)
    )
    assert resolution.rejected == ()
    # EMPTY = the shipped default: every desk whose loaded aperture shares a
    # map_version with a loaded layer table. The descriptor once named
    # "country_watch_ru:RU" / "country_watch_ar:AR" while those maps had loaded
    # under country_g20_ru / country_g20_ar, and the first live run (2026-09-24
    # 20:49Z) reported three pairs aperture_excluded/undeclared for a naming
    # slip. The desk ids are the maps' own, never the descriptor's.
    assert resolution.accepted["divergence_targets"] == []


def test_the_marker_constants_are_present():
    assert ld.LAYER_DIVERGENCE_VERSION == "layer_divergence.v1"
    assert (REPO_ROOT / "descriptors" / "analyst_layer_divergence.yaml").exists()


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("country_watch_ru", ("country_watch_ru", "")),
        ("country_watch_ru:RU", ("country_watch_ru", "RU")),
        ("country_watch_ru:ru", ("country_watch_ru", "RU")),
        ("country_watch_ru:RUS", None),
        (":RU", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_target_option(raw, expected):
    assert ld.parse_target_option(raw) == expected


# ---------------------------------------------------------------------------
# 9. The LIVE path — the real binding, against a real Postgres (0214)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def seeded(pg_pool, clean_tables):
    """A curated map for ZZ + a flat fortnight whose last two days go silent.

    ZZ is a reserved-for-private-use ISO code, so nothing in the live
    catalogue can collide with it.
    """
    await clean_tables("source_layers", "desk_apertures", "signals")
    async with pg_pool.acquire() as conn:
        for source_id, layer in (
            ("source.test.official", "official"),
            ("source.test.social", "social_digest"),
        ):
            await conn.execute(
                "INSERT INTO public.source_layers "
                "(source_id, country, layer, reason, map_version) "
                "VALUES ($1, 'ZZ', $2, 'seeded by the L2 test', 'layer_map_zz.t1')",
                source_id, layer,
            )
        for layer in LAYER_VOCAB:
            declared = (
                "present" if layer in ("official", "social_digest") else "unmeasured"
            )
            await conn.execute(
                "INSERT INTO public.desk_apertures "
                "(target_id, layer, declared, reason, map_version) "
                "VALUES ('country_watch_zz', $1, $2, '', 'layer_map_zz.t1')",
                layer, declared,
            )
        for offset in range(28):
            day = AS_OF - timedelta(days=offset)
            n_official = 0 if offset < 2 else 10
            for source_id, n in (
                ("source.test.official", n_official),
                ("source.test.social", 10),
            ):
                for k in range(n):
                    ts = datetime.combine(
                        day, datetime.min.time(), tzinfo=timezone.utc
                    ) + timedelta(hours=9, minutes=k)
                    await conn.execute(
                        "INSERT INTO public.signals "
                        "(source_id, fetched_at, created_at, geo, content_hash, "
                        " payload) VALUES ($1, $2, $2, ARRAY['ZZ']::text[], $3, "
                        " $4::jsonb)",
                        source_id, ts, f"{source_id}-{day}-{k}",
                        f'{{"title": "a distinct seeded headline {k} on {day}"}}',
                    )
    return pg_pool


class _Deps:
    def __init__(self, pool: Any) -> None:
        self.pg_pool = pool


async def test_live_run_through_the_real_binding_path(seeded):
    """THE REAL BINDING. Dispatched through ``deterministic.run_method`` by
    ``options['sub_handler']`` — the path the actor actually takes — not by
    calling ``ld.handle`` directly."""
    result = await run_method(
        [],
        {
            "sub_handler": "layer_divergence",
            "analyst_id": "layer_divergence",
            "as_of": AS_OF.isoformat(),
            "divergence_targets": ["country_watch_zz:ZZ"],
        },
        _Deps(seeded),
    )
    data = result.finding.data
    (target,) = data["targets"]
    assert target["target_id"] == "country_watch_zz"
    assert target["country"] == "ZZ"
    assert target["map_version"] == "layer_map_zz.t1"
    assert target["sources_mapped"] == 2
    assert target["rows_truncated"] is False
    # The counts came back off the real scan: 10 social every day, official
    # silent for the last two.
    daily = target["layers"]["social_digest"]["daily"]
    assert daily[AS_OF.isoformat()]["kept"] == 10
    assert target["layers"]["official"]["daily"][AS_OF.isoformat()]["kept"] == 0
    # …and the two-day rule fired on it.
    assert data["divergence_count"] == 1
    (div,) = data["divergences"]
    assert div["pair_id"] == "regime_public_gap"
    assert div["louder_layer"] == "social_digest"
    assert result.force_trace_only is False
    assert data["classification_audit"] == ld.CLASSIFICATION_AUDIT_NOTE


async def test_live_desk_resolution_joins_on_map_version(seeded):
    """With NO named targets the population is every desk whose aperture
    shares a map_version with a loaded layer table — the recorded fact, never
    a country parsed out of the target_id's spelling."""
    result = await run_method(
        [],
        {
            "sub_handler": "layer_divergence",
            "analyst_id": "layer_divergence",
            "as_of": AS_OF.isoformat(),
        },
        _Deps(seeded),
    )
    targets = result.finding.data["targets"]
    assert [t["target_id"] for t in targets] == ["country_watch_zz"]
    assert targets[0]["country"] == "ZZ"


async def test_live_named_target_with_no_loaded_aperture_is_all_undeclared(seeded):
    """The ':<CC>' escape hatch: the layer table alone is measurable, and the
    missing aperture reads as six UNDECLARED layers that exclude every pair."""
    async with seeded.acquire() as conn:
        await conn.execute("DELETE FROM public.desk_apertures")
    result = await run_method(
        [],
        {
            "sub_handler": "layer_divergence",
            "analyst_id": "layer_divergence",
            "as_of": AS_OF.isoformat(),
            "divergence_targets": ["country_watch_zz:ZZ"],
        },
        _Deps(seeded),
    )
    (target,) = result.finding.data["targets"]
    assert len(target["aperture"]["undeclared"]) == len(LAYER_VOCAB)
    assert target["aperture"]["present"] == []
    assert all(
        p["no_fire_reason"] == "aperture_excluded" for p in target["pairs"]
    )
    assert result.finding.data["divergence_count"] == 0


async def test_live_unresolvable_target_is_reported_not_dropped(seeded):
    result = await run_method(
        [],
        {
            "sub_handler": "layer_divergence",
            "analyst_id": "layer_divergence",
            "as_of": AS_OF.isoformat(),
            "divergence_targets": ["country_watch_nowhere"],
        },
        _Deps(seeded),
    )
    unresolved = result.finding.data["targets_unresolved"]
    assert [u["target_id"] for u in unresolved] == ["country_watch_nowhere"]
    assert "load the map" in unresolved[0]["reason"]
    assert result.finding.data["targets"] == []


async def test_live_signal_scan_excludes_other_countries(seeded):
    """The geo predicate is what makes the count about the COUNTRY rather than
    about the outlet: the same mapped source publishing about elsewhere must
    not enter this country's layer."""
    async with seeded.acquire() as conn:
        for k in range(50):
            await conn.execute(
                "INSERT INTO public.signals "
                "(source_id, fetched_at, created_at, geo, content_hash, payload) "
                "VALUES ('source.test.official', $1, $1, ARRAY['XX']::text[], "
                " $2, $3::jsonb)",
                datetime.combine(AS_OF, datetime.min.time(), tzinfo=timezone.utc)
                + timedelta(hours=9, minutes=k),
                f"elsewhere-{k}",
                f'{{"title": "an entirely unrelated headline {k} elsewhere"}}',
            )
    result = await run_method(
        [],
        {
            "sub_handler": "layer_divergence",
            "analyst_id": "layer_divergence",
            "as_of": AS_OF.isoformat(),
            "divergence_targets": ["country_watch_zz:ZZ"],
        },
        _Deps(seeded),
    )
    (target,) = result.finding.data["targets"]
    # Still silent — the 50 XX rows belong to another country's read.
    assert target["layers"]["official"]["daily"][AS_OF.isoformat()]["kept"] == 0
    assert result.finding.data["divergence_count"] == 1
