# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``layer_divergence`` sub-handler — Program 6 L2, the divergence-baseline unit.

THE THESIS (docs/DIRECTION.md, "The layered source fan-out"; docs/LAYERS.md).
Per target country the platform ingests the same stack of source LAYERS, and
the finding is the CHANGE in that country's own layer-to-layer divergence —
never the raw gap, which every country has. A country whose official layer has
always been quieter than its social digest is not news; a country whose
official layer goes quiet *relative to its own fourteen-day baseline*, and
stays there a second day, is.

This module is the unit that turns a LOADED map (``source_layers`` /
``desk_apertures``, migration 0214 — L0) into numbers and findings,
deterministically and with real citations. It is the PROOF ROUND's instrument:
if the gap-change findings say nothing the desks did not, the program stops at
zero cost, and the receipt below is what makes that verdict readable.

THE FIVE STEPS
--------------

1. **COUNTS.** Signals per layer per UTC day over ``window_days`` (28), with
   wire copies FOLDED to one (see "THE FOLD"). A signal joins a layer when its
   ``source_id`` is in the country's OPEN layer map under that layer AND its
   ``geo`` names the country — the map says which layer an outlet reads as, the
   geo says the item is about this country. Counting an outlet's whole output
   would measure the outlet, not the country's information environment.

2. **BASELINE.** Per PAIR OF INTEREST (:data:`PAIRS_OF_INTEREST`) the day's
   smoothed log-ratio, and the rolling median + MAD of the ``baseline_days``
   (14) days BEFORE it. The statistic is
   ``log2((a + ALPHA) / (b + ALPHA))`` with :data:`_ALPHA` = 0.5 — a continuity
   correction, so a silent layer is a finite number rather than a division by
   zero, and swapping the two layers negates the value exactly (a symmetric
   gap needs a symmetric statistic). Median and MAD rather than mean and sd
   because a single surge day must not re-base the thing the surge is measured
   against.

3. **DIVERGENCE.** ``z = (log_ratio - median) / scale`` where ``scale`` is the
   normal-consistent ``1.4826 * MAD``, floored at ``mad_floor``. A finding
   fires only when ``|z| >= z_threshold`` on the day under test AND on the day
   before it AND both days' z carry the SAME SIGN — :data:`_CONSECUTIVE_DAYS`
   is 2 and is NOT an option: the two-day rule IS the measure, not a knob. The
   move is named ``widening`` or ``narrowing`` by comparing the day's |gap| to
   the baseline's, and the layer that got relatively louder is named too.

4. **THE APERTURE.** A layer declared ``absent`` contributes NO count and the
   finding says so (with the operator's reason). ``unmeasured`` is excluded and
   named. A layer with NO row for this target is ``undeclared`` — a third,
   distinct exclusion, because docs/LAYERS.md is emphatic that undeclared is
   not the same thing as unmeasured. A pair with either side excluded is
   SKIPPED and the receipt says which side and why. When the map carries
   sources for a layer the aperture declares ``absent``, the receipt reports
   the suppressed count under ``counts_suppressed_by_aperture`` — the map and
   the declaration disagreeing is itself worth reading.

5. **THE RECEIPT.** Per-layer daily counts, every baseline, every pair, and —
   on a quiet run — WHY no finding fired, per pair, by name. A run that fires
   nothing is ``force_trace_only`` (the ``indicator_tracker`` contract), so the
   feed never repeats "nothing changed" every tick; the receipt still lands in
   ``analyst_traces.output_payload``, which is where the proof round reads the
   fourteen quiet days from.

THE THREE MODULES
-----------------

``_layer_fold`` holds the COUNT half — the row shapes and the wire fold that
turn a day's raw rows into a layer's honest count (a wire copy under two
mastheads counts once, folded WITHIN one (target, layer, day) bucket and
nowhere else, because the same dispatch appearing in ``domestic_press`` AND
``foreign_press`` is the narrative-control pair's actual subject rather than a
duplicate). ``_layer_divergence_reads`` holds the four queries and the
synthetic path. This module holds the STATISTIC, the aperture, the finding and
the handler, and reaches a database only through those four functions. Every
``options.get`` read lives HERE, so the X-1 reachability sweep sees all of them
without a ``_DELEGATES`` entry.

WHAT THIS ROUND DOES NOT BUILD
-------------------------------

No new table: the series rides ``analyst_outputs.data`` exactly as the
indicator tracker's does. What a table would need if the proof passes is stated
in the lane report, not guessed at here.

No classification audit. A source filed under the wrong layer is a DIRECTIONAL
error — it does not blur the measure, it moves the gap the wrong way — and the
sampled audit that would bound that error rate is the NEXT lane. Declared as
**SEAMS #60**; every receipt carries
:data:`CLASSIFICATION_AUDIT_NOTE` verbatim so no reader of a divergence finding
can mistake an un-audited map for an audited one.

``deps=None`` runs the synthetic (no-DB) path over pre-shaped ``inputs``
bundles — see :func:`collect_divergences`.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID

from ._layer_divergence_reads import (
    DAY_BASIS_CHOICES,
    DEFAULT_DAY_BASIS,
    fetch_bundle,
    last_emitted_body,
    parse_target_option,
    resolve_desks,
    synthetic_bundles,
)
from ._layer_fold import DayCount, SignalItem, TargetBundle, count_layer_days
from ...layers._vocab import LAYER_VOCAB
from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "layer_divergence"

#: H12 — the instrument revision (docs/ANALYSIS.md §10.9). Bumped when the
#: STATISTIC moves: the log-ratio's continuity constant, the median/MAD
#: baseline, the MAD scale, the two-day rule, the severity ladder or the fold.
#: A diff across a bump is an instrument change, never a world change.
METHOD_VERSION: str = "layer_divergence/2026-09.1"

#: The PAYLOAD-SHAPE version of the series + receipt this handler writes into
#: ``analyst_outputs.data`` (the ``wire_map.v1`` / ``region_rollup.v1``
#: convention). Separate from :data:`METHOD_VERSION` on purpose: a reader
#: parsing the series needs to know whether the KEYS moved, and an analyst
#: comparing two numbers needs to know whether the STATISTIC moved. Those are
#: different questions and they change on different days.
LAYER_DIVERGENCE_VERSION: str = "layer_divergence.v1"

#: Stamped verbatim on every receipt — see "WHAT THIS ROUND DOES NOT BUILD".
CLASSIFICATION_AUDIT_NOTE: str = (
    "unaudited: no sampled layer-classification audit has been run over this "
    "map (SEAMS #60). A source filed under the wrong layer is a DIRECTIONAL "
    "error in every number below, not noise."
)


@dataclass(frozen=True)
class LayerPair:
    """One layer-to-layer gap worth watching, and what it is a gap ABOUT."""

    pair_id: str
    a: str
    b: str
    meaning: str


#: The three pairs the program is about. Each is a RATIO of layer a to layer b,
#: so the sign of the log-ratio always means "a is relatively louder".
PAIRS_OF_INTEREST: tuple[LayerPair, ...] = (
    LayerPair(
        "regime_public_gap", "official", "social_digest",
        "the regime-public gap — what the state publishes against what the "
        "monitored channels carry",
    ),
    LayerPair(
        "narrative_control", "domestic_press", "foreign_press",
        "narrative control — how much the domestic press says about this "
        "country against how much the outside press does",
    ),
    LayerPair(
        "credibility_gap", "official", "public_data",
        "the credibility gap — state publication against structured "
        "third-party data on the same country",
    ),
)

#: Continuity correction on both sides of the ratio (Haldane-Anscombe). 0.5 is
#: the conventional value and it is what makes a SILENT layer representable: a
#: day with 0 official and 12 social items is log2(0.5/12.5) = -4.6, a large
#: finite number, rather than an exclusion that would hide exactly the day the
#: measure exists to catch. NOT an option — it defines the statistic.
_ALPHA: float = 0.5

#: MAD -> standard-deviation-equivalent scale for a normal distribution. The
#: textbook constant; named rather than inlined so the z is re-derivable by a
#: reader who does not already know it.
_MAD_TO_SIGMA: float = 1.4826

#: The two-day rule. A CONSTANT, not an option: "|z| over threshold on two
#: consecutive days" is the measure's definition of a real move, and a knob
#: that could set it to 1 would silently turn this instrument into the
#: single-day noise detector the program was designed to avoid.
_CONSECUTIVE_DAYS: int = 2

#: Fewer baseline days than this is not a baseline, it is a guess — the day is
#: reported with ``no_fire_reason='baseline_thin'`` and never fires.
_BASELINE_MIN_DAYS: int = 7

_DEFAULT_WINDOW_DAYS: int = 28
_DEFAULT_BASELINE_DAYS: int = 14
_DEFAULT_Z_THRESHOLD: float = 2.0
#: Floor on the MAD-derived scale, in log2 units. Without it a pair whose ratio
#: sat at EXACTLY one value for fourteen days has MAD 0 and fires on any move
#: at all, which is the noise this instrument exists to refuse. 0.2 in log2 is
#: about a 15% ratio move — below that, a flat pair is treated as flat.
_DEFAULT_MAD_FLOOR: float = 0.2
#: A layer carrying fewer than this many folded items on the day is THIN, and
#: the finding says so. A ratio between two handfuls is arithmetic, not
#: evidence.
_DEFAULT_THIN_MIN_PER_DAY: int = 5
#: Folded items on the smaller side at which the divergence earns full
#: confidence; below it confidence scales down linearly to :data:`_MIN_CONF`.
_DEFAULT_CONFIDENCE_FULL_N: int = 20
_MIN_CONF: float = 0.1
_DEFAULT_MAX_SIGNAL_ROWS: int = 20_000
_DEFAULT_MAX_CITATIONS_PER_LAYER: int = 3
#: Bound on the O(n^2) declared-pair walk inside ONE (layer, day) bucket.
_DEFAULT_MAX_FOLD_ROWS: int = 60
#: Hard ceiling on divergences carried in one finding's body/data.
_MAX_DIVERGENCES: int = 50

#: Aperture states, and the exclusion code each produces.
_EXCLUSION_FOR_DECLARED: Mapping[str, str] = {
    "absent": "absent",
    "unmeasured": "unmeasured",
}
_UNDECLARED: str = "undeclared"


# ---------------------------------------------------------------------------
# 2. BASELINE
# ---------------------------------------------------------------------------


def log_ratio(a: int, b: int) -> float:
    """``log2((a + ALPHA) / (b + ALPHA))`` — see the module banner."""
    return math.log2((a + _ALPHA) / (b + _ALPHA))


def median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def mad(values: Sequence[float], centre: float) -> float:
    return median([abs(v - centre) for v in values])


@dataclass(frozen=True)
class Baseline:
    """The rolling centre and spread a day's log-ratio is measured against."""

    n: int
    centre: float | None = None
    raw_mad: float | None = None
    scale: float | None = None
    floored: bool = False

    @property
    def usable(self) -> bool:
        return self.centre is not None and self.scale is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "centre": _round(self.centre),
            "mad": _round(self.raw_mad),
            "scale": _round(self.scale),
            "scale_floored": self.floored,
        }


def baseline_of(values: Sequence[float], *, mad_floor: float) -> Baseline:
    """Median/MAD over the trailing log-ratios, with the scale floor applied."""
    if len(values) < _BASELINE_MIN_DAYS:
        return Baseline(n=len(values))
    centre = median(values)
    raw = mad(values, centre)
    scaled = _MAD_TO_SIGMA * raw
    floored = scaled < mad_floor
    return Baseline(
        n=len(values),
        centre=centre,
        raw_mad=raw,
        scale=max(scaled, mad_floor),
        floored=floored,
    )


def _round(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(float(value), places)


# ---------------------------------------------------------------------------
# 3. DIVERGENCE — the two-day rule
# ---------------------------------------------------------------------------


@dataclass
class DayPoint:
    """One day of one pair: the two counts, the ratio, and the z against the
    baseline built from the days BEFORE it."""

    day: str
    a: int
    b: int
    log_ratio: float
    blank: bool
    baseline: Baseline
    z: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "a": self.a,
            "b": self.b,
            "log_ratio": _round(self.log_ratio),
            "blank": self.blank,
            "z": _round(self.z),
            "baseline": self.baseline.to_dict(),
        }


def _severity_for(z: float) -> str:
    """The severity ladder. Deliberately tops out at ``high``.

    A move in how much each layer is SAYING is never on its own a critical
    world event; stamping ``critical`` would be the instrument claiming
    something about the world that it has not measured.
    """
    az = abs(z)
    if az >= 5.0:
        return "high"
    if az >= 3.5:
        return "elevated"
    return "moderate"


def _confidence_for(a: int, b: int, *, full_n: int) -> float:
    """Confidence from the COUNTS — the thinner side governs."""
    if full_n <= 0:
        return _MIN_CONF
    share = min(a, b) / float(full_n)
    return round(max(_MIN_CONF, min(1.0, share)), 3)


def evaluate_pair(
    pair: LayerPair,
    counts: Mapping[str, Mapping[str, DayCount]],
    *,
    days: Sequence[str],
    baseline_days: int,
    z_threshold: float,
    mad_floor: float,
) -> tuple[list[DayPoint], dict[str, Any] | None, str]:
    """Walk one pair across the window.

    Returns ``(series, divergence_or_None, no_fire_reason)``. ``no_fire_reason``
    is "" when a divergence fired, and otherwise NAMES why — the receipt's
    whole job on a quiet day.
    """
    a_days = counts.get(pair.a, {})
    b_days = counts.get(pair.b, {})

    series: list[DayPoint] = []
    history: list[float] = []
    for day in days:
        a = a_days.get(day, DayCount()).kept
        b = b_days.get(day, DayCount()).kept
        blank = a == 0 and b == 0
        lr = log_ratio(a, b)
        # The baseline is the trailing window of NON-BLANK days strictly before
        # this one. A day on which NEITHER layer carried anything is not
        # evidence about the country — it is a quiet Sunday or a stalled poll —
        # so it neither sets the baseline nor is tested against one.
        base = baseline_of(history[-baseline_days:], mad_floor=mad_floor)
        point = DayPoint(
            day=day, a=a, b=b, log_ratio=lr, blank=blank, baseline=base
        )
        if not blank and base.usable:
            point.z = (lr - float(base.centre)) / float(base.scale)
        series.append(point)
        if not blank:
            history.append(lr)

    if len(series) < _CONSECUTIVE_DAYS:
        return series, None, "window_too_short"

    tail = series[-_CONSECUTIVE_DAYS:]
    if any(p.blank for p in tail):
        return series, None, "blank_day"
    if any(p.z is None for p in tail):
        thin = any(not p.baseline.usable for p in tail)
        return series, None, "baseline_thin" if thin else "no_z"

    zs = [float(p.z) for p in tail]
    if any(abs(z) < z_threshold for z in zs):
        return series, None, "below_threshold"
    if not (all(z > 0 for z in zs) or all(z < 0 for z in zs)):
        return series, None, "sign_flipped"

    today = tail[-1]
    z_today = float(today.z)
    base_centre = float(today.baseline.centre)
    direction = "widening" if abs(today.log_ratio) > abs(base_centre) else "narrowing"
    louder = pair.a if today.log_ratio > 0 else pair.b
    quieter = pair.b if today.log_ratio > 0 else pair.a

    divergence = {
        "pair_id": pair.pair_id,
        "meaning": pair.meaning,
        "layer_a": pair.a,
        "layer_b": pair.b,
        "day": today.day,
        "direction": direction,
        "louder_layer": louder,
        "quieter_layer": quieter,
        "counts": {pair.a: today.a, pair.b: today.b},
        "log_ratio": _round(today.log_ratio),
        "baseline": today.baseline.to_dict(),
        "z": _round(z_today),
        "z_prior_days": [_round(z) for z in zs[:-1]],
        "consecutive_days": _CONSECUTIVE_DAYS,
        "severity": _severity_for(z_today),
    }
    return series, divergence, ""


# ---------------------------------------------------------------------------
# 4. THE APERTURE
# ---------------------------------------------------------------------------


@dataclass
class ApertureView:
    """One desk's declared reach, split into what may and may not be read."""

    present: list[str] = field(default_factory=list)
    excluded: dict[str, tuple[str, str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"present": sorted(self.present)}
        for code in ("absent", "unmeasured", _UNDECLARED):
            rows = [
                {"layer": layer, "reason": reason}
                for layer, (c, reason) in sorted(self.excluded.items())
                if c == code
            ]
            out[code] = rows
        return out


def aperture_view(declared: Mapping[str, tuple[str, str]]) -> ApertureView:
    """Split :data:`LAYER_VOCAB` into present / absent / unmeasured /
    undeclared for one target. Every layer lands in exactly one bucket."""
    view = ApertureView()
    for layer in LAYER_VOCAB:
        row = declared.get(layer)
        if row is None:
            view.excluded[layer] = (_UNDECLARED, "")
            continue
        state, reason = row
        if state == "present":
            view.present.append(layer)
            continue
        view.excluded[layer] = (
            _EXCLUSION_FOR_DECLARED.get(state, _UNDECLARED), reason or ""
        )
    return view


# ---------------------------------------------------------------------------
# The per-target pass
# ---------------------------------------------------------------------------


def window_days_list(as_of: date, window_days: int) -> list[str]:
    """The UTC day keys in the window, oldest first, ending at ``as_of``."""
    n = max(1, int(window_days))
    return [
        (as_of - timedelta(days=n - 1 - i)).isoformat() for i in range(n)
    ]


@dataclass
class _Cfg:
    """Resolved run knobs — one object rather than eleven parameters."""

    window_days: int = _DEFAULT_WINDOW_DAYS
    baseline_days: int = _DEFAULT_BASELINE_DAYS
    z_threshold: float = _DEFAULT_Z_THRESHOLD
    mad_floor: float = _DEFAULT_MAD_FLOOR
    thin_min_per_day: int = _DEFAULT_THIN_MIN_PER_DAY
    confidence_full_n: int = _DEFAULT_CONFIDENCE_FULL_N
    max_citations_per_layer: int = _DEFAULT_MAX_CITATIONS_PER_LAYER
    max_fold_rows: int = _DEFAULT_MAX_FOLD_ROWS
    as_of: date = field(default_factory=lambda: datetime.now(timezone.utc).date())


def _citations_for(
    reps: Mapping[str, Mapping[str, list[SignalItem]]],
    *,
    layers: Iterable[str],
    day: str,
    cap: int,
    start_ordinal: int,
) -> list[dict[str, Any]]:
    """The day's top folded items per layer, as ordinary signal citations.

    "Top" is the fold's own deterministic order — newest distinct dispatch
    first — because this instrument has no relevance score and inventing one
    would be a claim it cannot back. Each entry is a REAL representative of a
    counted dispatch, so every citation resolves.
    """
    out: list[dict[str, Any]] = []
    ordinal = start_ordinal
    for layer in layers:
        for item in (reps.get(layer, {}).get(day) or ())[:cap]:
            out.append({
                "ordinal": ordinal,
                "marker": f"[{ordinal}]",
                "signal_id": item.signal_id,
                "source_id": item.source_id,
                "title": item.title[:512] or None,
                "layer": layer,
                "day": day,
            })
            ordinal += 1
    return out


def evaluate_target(bundle: TargetBundle, cfg: _Cfg) -> dict[str, Any]:
    """The whole pass for ONE desk: counts, apertures, pairs, divergences."""
    days = window_days_list(cfg.as_of, cfg.window_days)
    counts, reps = count_layer_days(
        bundle, days=days, max_fold_rows=cfg.max_fold_rows
    )
    view = aperture_view(bundle.apertures)

    # An `absent` layer contributes NO count. When the map nevertheless carries
    # sources for it, the suppression is REPORTED rather than silently applied
    # — the map and the declaration disagreeing is itself a finding about the
    # curation, and hiding it would be the exact silent-agreement failure the
    # aperture exists to rule out.
    suppressed: dict[str, int] = {}
    for layer, (code, _reason) in view.excluded.items():
        if code != "absent":
            continue
        total = sum(d.kept for d in counts.get(layer, {}).values())
        if total:
            suppressed[layer] = total
        counts.pop(layer, None)
        reps.pop(layer, None)

    layer_rows: dict[str, Any] = {}
    thin_days: dict[str, list[str]] = {}
    for layer in LAYER_VOCAB:
        state = (
            "present" if layer in view.present else view.excluded[layer][0]
        )
        daily = counts.get(layer, {})
        thin = [
            day for day in days
            if daily.get(day, DayCount()).kept < cfg.thin_min_per_day
        ]
        if layer in view.present:
            thin_days[layer] = thin
        layer_rows[layer] = {
            "declared": state,
            "reason": view.excluded.get(layer, ("", ""))[1],
            "sources_mapped": sum(
                1 for lay in bundle.layer_map.values() if lay == layer
            ),
            "daily": {
                day: {
                    "kept": daily.get(day, DayCount()).kept,
                    "folded": daily.get(day, DayCount()).folded,
                    "raw": daily.get(day, DayCount()).raw,
                }
                for day in days
            },
            "thin_days": len(thin),
        }

    pair_rows: list[dict[str, Any]] = []
    divergences: list[dict[str, Any]] = []
    for pair in PAIRS_OF_INTEREST:
        missing = [
            layer for layer in (pair.a, pair.b) if layer not in view.present
        ]
        if missing:
            pair_rows.append({
                "pair_id": pair.pair_id,
                "layer_a": pair.a,
                "layer_b": pair.b,
                "evaluable": False,
                "no_fire_reason": "aperture_excluded",
                "excluded_layers": [
                    {
                        "layer": layer,
                        "state": view.excluded[layer][0],
                        "reason": view.excluded[layer][1],
                    }
                    for layer in missing
                ],
            })
            continue

        series, divergence, reason = evaluate_pair(
            pair, counts,
            days=days,
            baseline_days=cfg.baseline_days,
            z_threshold=cfg.z_threshold,
            mad_floor=cfg.mad_floor,
        )
        row: dict[str, Any] = {
            "pair_id": pair.pair_id,
            "layer_a": pair.a,
            "layer_b": pair.b,
            "evaluable": True,
            "no_fire_reason": reason,
            "series": [p.to_dict() for p in series],
        }
        pair_rows.append(row)
        if divergence is None:
            continue

        day = divergence["day"]
        thin_now = sorted(
            layer for layer in (pair.a, pair.b)
            if counts.get(layer, {}).get(day, DayCount()).kept
            < cfg.thin_min_per_day
        )
        divergence.update({
            "target_id": bundle.target_id,
            "country": bundle.country,
            "map_version": bundle.map_version,
            "thin": bool(thin_now),
            "thin_layers": thin_now,
            "thin_min_per_day": cfg.thin_min_per_day,
            "confidence": _confidence_for(
                divergence["counts"][pair.a],
                divergence["counts"][pair.b],
                full_n=cfg.confidence_full_n,
            ),
            "citations": _citations_for(
                reps,
                layers=(pair.a, pair.b),
                day=day,
                cap=cfg.max_citations_per_layer,
                start_ordinal=1,
            ),
        })
        divergences.append(divergence)

    return {
        "target_id": bundle.target_id,
        "country": bundle.country,
        "map_version": bundle.map_version,
        "sources_mapped": len(bundle.layer_map),
        "rows_scanned": len(bundle.signals),
        "rows_truncated": bundle.rows_truncated,
        "aperture": view.to_dict(),
        "counts_suppressed_by_aperture": suppressed,
        "layers": layer_rows,
        "pairs": pair_rows,
        "divergence_count": len(divergences),
        "_divergences": divergences,
    }


def collect_divergences(
    bundles: Sequence[TargetBundle], cfg: _Cfg
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run every bundle. Returns ``(target_receipts, divergences)``.

    Divergences are ordered deterministically — severity worst-first, then
    ``(target_id, pair_id)`` — so the finding body is byte-stable across two
    runs over the same data, which is what makes the dedup check downstream
    mean what it says.
    """
    receipts: list[dict[str, Any]] = []
    divergences: list[dict[str, Any]] = []
    for bundle in sorted(bundles, key=lambda b: (b.target_id, b.country)):
        receipt = evaluate_target(bundle, cfg)
        divergences.extend(receipt.pop("_divergences"))
        receipts.append(receipt)

    rank = {"high": 0, "elevated": 1, "moderate": 2}
    divergences.sort(
        key=lambda d: (
            rank.get(str(d["severity"]), 9),
            -abs(float(d["z"] or 0.0)),
            str(d["target_id"]),
            str(d["pair_id"]),
        )
    )
    return receipts, divergences[:_MAX_DIVERGENCES]


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _line_for(d: Mapping[str, Any]) -> str:
    thin = " [thin]" if d.get("thin") else ""
    return (
        f"- [{d['target_id']}/{d['country']}] {d['pair_id']} {d['direction']}: "
        f"{d['louder_layer']} {d['counts'][d['layer_a']]}"
        f"/{d['counts'][d['layer_b']]} vs baseline, z={d['z']} "
        f"on {d['day']} and the day before{thin}"
    )


def build_finding(
    receipts: Sequence[Mapping[str, Any]],
    divergences: Sequence[Mapping[str, Any]],
    cfg: _Cfg,
    *,
    unresolved: Sequence[Mapping[str, Any]] = (),
    warnings: Sequence[str] = (),
) -> FindingPayload:
    """The per-run summary finding + the receipt that makes a quiet run
    readable."""
    n = len(divergences)
    widening = [d for d in divergences if d.get("direction") == "widening"]
    narrowing = [d for d in divergences if d.get("direction") == "narrowing"]
    thin = [d for d in divergences if d.get("thin")]

    if n:
        worst = divergences[0]
        severity = str(worst["severity"])
        title = (
            f"Layer divergence: {n} gap change(s) across "
            f"{len(receipts)} desk(s) — {len(widening)} widening, "
            f"{len(narrowing)} narrowing"
        )
        lines = [_line_for(d) for d in divergences]
        if thin:
            lines.append(
                f"THIN: {len(thin)} of {n} rest on a layer carrying fewer than "
                f"{cfg.thin_min_per_day} folded items on the day — the ratio is "
                "arithmetic over a handful, read it as such."
            )
        body = "\n".join(lines)
        confidence = min(float(d.get("confidence", _MIN_CONF)) for d in divergences)
    else:
        severity = "info"
        title = (
            f"Layer divergence: no gap change across {len(receipts)} desk(s)"
        )
        reasons = sorted({
            str(p.get("no_fire_reason") or "")
            for r in receipts for p in r.get("pairs", ())
            if p.get("no_fire_reason")
        })
        body = (
            "No layer pair moved |z| >= "
            f"{cfg.z_threshold} on two consecutive days. Pairs did not fire "
            f"for: {', '.join(reasons) or 'nothing evaluated'}."
        )
        confidence = 1.0

    tags = ["deterministic", SUB_HANDLER_NAME, f"severity:{severity}"]
    if thin:
        tags.append("thin_layer")

    citations: list[dict[str, Any]] = []
    ordinal = 1
    for d in divergences:
        for entry in d.get("citations", ()):
            citations.append({**entry, "ordinal": ordinal, "marker": f"[{ordinal}]"})
            ordinal += 1

    structural_claims = [
        {
            "id": "divergence_partition",
            "statement": (
                f"divergence_count ({n}) = widening ({len(widening)}) + "
                f"narrowing ({len(narrowing)})"
            ),
            "op": "sum",
            "asserted": n,
            "basis": [len(widening), len(narrowing)],
        },
        {
            "id": "desks_evaluated_distinct",
            "statement": (
                f"targets_evaluated ({len(receipts)}) = distinct target_id "
                "among the per-desk receipts"
            ),
            "op": "distinct_count",
            "asserted": len(receipts),
            "basis": [str(r.get("target_id")) for r in receipts],
        },
    ]

    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=confidence,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "method_version": METHOD_VERSION,
            "payload_schema": LAYER_DIVERGENCE_VERSION,
            "as_of": cfg.as_of.isoformat(),
            "window_days": cfg.window_days,
            "baseline_days": cfg.baseline_days,
            "z_threshold": cfg.z_threshold,
            "mad_floor": cfg.mad_floor,
            "consecutive_days": _CONSECUTIVE_DAYS,
            "thin_min_per_day": cfg.thin_min_per_day,
            "severity": severity,
            "divergence_count": n,
            "widening_count": len(widening),
            "narrowing_count": len(narrowing),
            "thin_count": len(thin),
            "divergences": list(divergences),
            "targets_evaluated": len(receipts),
            "targets": list(receipts),
            "targets_unresolved": list(unresolved),
            "classification_audit": CLASSIFICATION_AUDIT_NOTE,
            "warnings": list(warnings),
            "citations": citations,
            "structural_claims": structural_claims,
        },
    )


def _cited_signal_ids(divergences: Sequence[Mapping[str, Any]]) -> list[UUID]:
    """The REAL signal uuids this run read, de-duplicated, in order.

    ``AnalystMethodResult.derived_from`` is documented as "the signal UUIDs
    this finding was reasoned over", and these are exactly that: every id is a
    fold representative of a dispatch that entered a count. An unparseable id
    contributes nothing rather than a fabricated anchor.
    """
    seen: set[UUID] = set()
    out: list[UUID] = []
    for d in divergences:
        for entry in d.get("citations", ()):
            raw = entry.get("signal_id")
            if not raw:
                continue
            try:
                uid = UUID(str(raw))
            except (ValueError, AttributeError, TypeError):
                continue
            if uid not in seen:
                seen.add(uid)
                out.append(uid)
    return out


def resolve_config(options: Mapping[str, Any]) -> tuple[_Cfg, list[str]]:
    """Resolve the run knobs, naming anything that degraded to its default.

    Every key below is declared in ``handler_options_programs
    .LAYER_DIVERGENCE_OPTIONS`` — the X-1 catalog validates TYPE and RANGE
    before a descriptor value ever reaches here, so this function only has to
    resolve the shapes the catalog cannot express (an ISO date).
    """
    warnings: list[str] = []
    cfg = _Cfg(
        window_days=int(options.get("window_days", _DEFAULT_WINDOW_DAYS)),
        baseline_days=int(options.get("baseline_days", _DEFAULT_BASELINE_DAYS)),
        z_threshold=float(options.get("z_threshold", _DEFAULT_Z_THRESHOLD)),
        mad_floor=float(options.get("mad_floor", _DEFAULT_MAD_FLOOR)),
        thin_min_per_day=int(
            options.get("thin_min_per_day", _DEFAULT_THIN_MIN_PER_DAY)
        ),
        confidence_full_n=int(
            options.get("confidence_full_n", _DEFAULT_CONFIDENCE_FULL_N)
        ),
        max_citations_per_layer=int(
            options.get("max_citations_per_layer", _DEFAULT_MAX_CITATIONS_PER_LAYER)
        ),
        max_fold_rows=int(options.get("max_fold_rows", _DEFAULT_MAX_FOLD_ROWS)),
    )
    raw_as_of = options.get("as_of")
    if raw_as_of:
        try:
            cfg.as_of = date.fromisoformat(str(raw_as_of)[:10])
        except ValueError:
            warnings.append(f"as_of {raw_as_of!r} is not an ISO date — using today")
    if cfg.baseline_days >= cfg.window_days:
        warnings.append(
            f"baseline_days ({cfg.baseline_days}) >= window_days "
            f"({cfg.window_days}) — no day in the window has a full baseline"
        )
    return cfg, warnings

# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see the module docstring.

    ``deps`` is the analyst pool bundle (``deps.pg_pool``); ``deps=None`` runs
    the synthetic path over pre-shaped ``inputs`` bundles, no DB, for the unit
    tests.

    Options
    -------
    divergence_targets:
        Desks to measure, as ``"<target_id>"`` or ``"<target_id>:<CC>"``.
        EMPTY (the shipped default) means every desk whose loaded aperture
        shares a ``map_version`` with a loaded layer table — so the sweep
        widens on its own as maps land, and measures nothing while the tables
        are empty.
    window_days / baseline_days / z_threshold / mad_floor:
        The statistic. See the module banner.
    thin_min_per_day / confidence_full_n:
        The honesty knobs — what counts as a thin layer, and how many folded
        items on the smaller side earn full confidence.
    day_basis:
        Which timestamp column buckets a UTC day. Choice-locked.
    as_of:
        The ONE stamped day the run measures at, ISO-8601 date. Absent means
        today. Set it for a REPLAY of a past day — the whole run is a pure
        function of the window, so a replay of an unchanged window reproduces
        the same body byte for byte.
    max_signal_rows / max_citations_per_layer / max_fold_rows:
        The bounds. ``max_fold_rows`` bounds the O(n^2) declared-pair walk;
        rows past it still COUNT, they just do not fold (over-counting a
        layer, never under-counting it).
    """
    cfg, warnings = resolve_config(options)
    day_basis = str(options.get("day_basis", DEFAULT_DAY_BASIS))
    if day_basis not in DAY_BASIS_CHOICES:
        warnings.append(f"day_basis {day_basis!r} unknown — using fetched_at")
        day_basis = DEFAULT_DAY_BASIS
    max_rows = int(options.get("max_signal_rows", _DEFAULT_MAX_SIGNAL_ROWS))

    wanted: list[tuple[str, str]] = []
    for raw in options.get("divergence_targets") or ():
        parsed = parse_target_option(raw)
        if parsed is None:
            warnings.append(f"divergence_targets entry {raw!r} is malformed")
            continue
        wanted.append(parsed)

    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    unresolved: list[dict[str, Any]] = []
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                desks, unresolved = await resolve_desks(conn, wanted)
                bundles = [
                    await fetch_bundle(
                        conn,
                        target_id=target_id,
                        country=country,
                        map_version=map_version,
                        as_of=cfg.as_of,
                        window_days=cfg.window_days,
                        day_basis=day_basis,
                        max_rows=max_rows,
                    )
                    for target_id, country, map_version in desks
                ]
        except Exception as exc:  # noqa: BLE001 — degrade-not-drop
            logger.warning("layer_divergence.pool_failed err=%s", exc)
            bundles, unresolved = [], []
            warnings.append(f"pool read failed: {type(exc).__name__}")
    else:
        bundles = synthetic_bundles(inputs)

    receipts, divergences = collect_divergences(bundles, cfg)
    finding = build_finding(
        receipts, divergences, cfg, unresolved=unresolved, warnings=warnings
    )

    # A run that fired nothing is a RECEIPT, not news: suppress it from the
    # feed so an idempotent re-run does not repeat "no gap change" every tick
    # (the indicator_tracker contract). The receipt still lands in
    # ``analyst_traces.output_payload``, which is where the proof round reads
    # the quiet days' series from. On the live path a divergence set
    # byte-identical to the last EMITTED summary is likewise suppressed; any
    # dedup failure degrades to EMIT rather than to silence.
    analyst_id = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    force_trace_only = not divergences
    if divergences and pool is not None:
        try:
            force_trace_only = (
                await last_emitted_body(pool, analyst_id) == finding.body
            )
        except Exception as exc:  # noqa: BLE001 — degrade: emit rather than crash
            logger.warning("layer_divergence.dedup_check_failed err=%s", exc)
            force_trace_only = False

    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        force_trace_only=force_trace_only,
        derived_from=_cited_signal_ids(divergences),
    )


__all__ = [
    "Baseline",
    "CLASSIFICATION_AUDIT_NOTE",
    "DayCount",
    "DayPoint",
    "LAYER_DIVERGENCE_VERSION",
    "LayerPair",
    "METHOD_VERSION",
    "PAIRS_OF_INTEREST",
    "SUB_HANDLER_NAME",
    "SignalItem",
    "TargetBundle",
    "aperture_view",
    "baseline_of",
    "build_finding",
    "collect_divergences",
    "count_layer_days",
    "evaluate_pair",
    "evaluate_target",
    "fetch_bundle",
    "handle",
    "log_ratio",
    "mad",
    "median",
    "parse_target_option",
    "resolve_config",
    "window_days_list",
]
