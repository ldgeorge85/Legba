# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The assembled X-1 catalogs — ``HANDLER_OPTIONS`` and ``ANALYST_KIND_OPTIONS``
— split out of ``handler_options.py`` so that module can stay the OptionSpec
MACHINERY (the reserved-key table, the ``OptionReject``/``OptionResolution``
dataclasses, the resolve/degrade traversal) rather than also carrying
~1,250 lines of catalog data.

**Why this module exists.** ``handler_options.py`` sat at its own
module-size-gate ceiling, and every lane that added a knob or a kind fought
the line count — the events catalog, the programs spec tuple and Program 5's
inquiry catalog already moved to their own siblings for the same reason (see
``handler_options_events.py`` / ``handler_options_programs.py`` /
``handler_options_inquiry.py``). This module is the next brick: it is where
``HANDLER_OPTIONS`` and ``ANALYST_KIND_OPTIONS`` are actually BUILT — the
per-sub-handler and per-kind spec tuples, the catalog-local constructor
helpers (``_nonneg_int`` / ``_unit_float`` / ``_nonneg_float`` / ``_pos_float``
/ ``_flag`` / ``_edge_families``) that only this catalog calls, and the
splice-in of the four sibling catalogs (events / inquiry / programs, each
imported under a private alias exactly as ``handler_options.py`` used to
import them).

``handler_options.py`` imports both dicts from here and re-exports them
under the same names, so ``handler_options.HANDLER_OPTIONS`` /
``handler_options.ANALYST_KIND_OPTIONS`` keep resolving for every existing
importer and test — this is an internal split of the catalog's own data, not
new public surface.

**Byte-identical, deliberately.** Every key and every ``OptionSpec`` field
in both dicts — and therefore every ``known_option_names()`` /
``known_kind_option_names()`` answer — is unchanged by this move: pinned by
``tests/data_pkg/test_handler_options_catalog_split.py`` against a SHA-256
fingerprint of both dicts taken from the pre-split module.
"""

from __future__ import annotations

from .handler_options_base import (
    OptionSpec,
    _MAX_WINDOW_DAYS,
    _MAX_WINDOW_HOURS,
    _pos_int,
    _window_days,
    _window_hours,
)
from .handler_options_events import EVENTS_CATALOG as _EVENTS_CATALOG
from .handler_options_inquiry import INQUIRY_CATALOG as _INQUIRY_CATALOG
from .handler_options_programs import (
    EXTERNAL_GRADING_WIDTH_OPTIONS as _EXTERNAL_GRADING_WIDTH_OPTIONS,
    INQUIRY_KIND_OPTIONS as _INQUIRY_KIND_OPTIONS,
    NARRATIVE_SPREAD_BLOCK_OPTIONS as _NARRATIVE_SPREAD_BLOCK_OPTIONS,
    PROGRAM_CATALOG as _PROGRAM_CATALOG,
    RELATIONSHIP_REIFIER_OPTIONS as _RELATIONSHIP_REIFIER_OPTIONS,
)
import re


__all__ = ["ANALYST_KIND_OPTIONS", "HANDLER_OPTIONS"]


# ---------------------------------------------------------------------------
# Spec shape helper
# ---------------------------------------------------------------------------
#
# ``OptionKind`` and ``OptionSpec`` live in ``handler_options_base``; this
# module imports ``OptionSpec`` alone (for its own type hints below) and
# constructs no ``OptionKind`` literal directly. The one pattern guard that
# lived beside that banner in ``handler_options.py`` moved down with it —
#
#: Guards a string option that reaches an identifier position (a Qdrant
#: collection name). Conservative on purpose.
_IDENT_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


# ---------------------------------------------------------------------------
# Shared spec constructors (kept terse — the catalog below is long)
#
# ``_pos_int`` / ``_window_hours`` / ``_window_days`` (and the
# ``_MAX_WINDOW_HOURS`` / ``_MAX_WINDOW_DAYS`` constants they bound against)
# are imported from ``handler_options_base`` above — this catalog and
# ``handler_options_programs.py``'s three blocks both call them, so they live
# on the shared leaf rather than here. The constructors below are called only
# from THIS module's own catalog.
# ---------------------------------------------------------------------------


def _nonneg_int(name: str, doc: str, *, maximum: float | None = None) -> OptionSpec:
    return OptionSpec(name, "int", doc, minimum=0, maximum=maximum)


def _unit_float(name: str, doc: str) -> OptionSpec:
    """A probability / score floor: a number in [0.0, 1.0]."""
    return OptionSpec(name, "float", doc, minimum=0.0, maximum=1.0)


def _nonneg_float(name: str, doc: str, *, maximum: float | None = None) -> OptionSpec:
    return OptionSpec(name, "float", doc, minimum=0.0, maximum=maximum)


def _pos_float(name: str, doc: str, *, maximum: float | None = None) -> OptionSpec:
    return OptionSpec(
        name, "float", doc, minimum=0.0, minimum_inclusive=False, maximum=maximum
    )


def _flag(name: str, doc: str) -> OptionSpec:
    return OptionSpec(name, "bool", doc)


#: The closed `edge_family` vocabulary — migration 0143's CHECK constraint, and
#: `vocabulary_entries` carries the same four values. Bound as `choices` so a
#: descriptor naming a family that does not exist is REJECTED at validation
#: rather than silently producing an empty graph at runtime.
_EDGE_FAMILY_CHOICES = ("relation", "reference", "cooccurrence", "structural")


def _edge_families(doc: str) -> OptionSpec:
    return OptionSpec(
        "edge_families", "str_list", doc, choices=_EDGE_FAMILY_CHOICES)


# ---------------------------------------------------------------------------
# THE CATALOG — sub-handler name → its declared knobs
# ---------------------------------------------------------------------------
#
# Keyed by the ``SUB_HANDLERS`` name (which is what the runtime resolves into
# ``options['sub_handler']``), NOT by module name — two sub-handlers can share
# a module (signals_retention / analyst_traces_retention both delegate to
# ``_retention_sweep``) and must be independently configurable.
#
# An entry with an EMPTY tuple is a deliberate, tested statement: that handler
# reads no operator-settable option today. It is not an oversight, and it
# still degrades loudly if a descriptor tries to set one.

# Three per-feature blocks the 09-05 merge wave added — external grading
# width (W-9), desk_reference (A-1), research_measurement (R-D) — live in
# handler_options_programs.py (module-size gate follow-up) and are spliced
# back in below at the same keys (imported at the top of the file now that
# the cycle fix — handler_options_base.py — makes this a genuine one-way
# dependency instead of an order-dependent partial circular import; see that
# module's docstring). Aliased to underscore-prefixed names purely so this
# module's PUBLIC surface (``dir(handler_options)`` minus dunders/
# underscores) stays exactly what it was before the split — the three
# constants are an implementation seam, not part of the catalog's contract.

HANDLER_OPTIONS: dict[str, tuple[OptionSpec, ...]] = {
    # -- alerting / triggers -------------------------------------------------
    "alert_trigger_scan": (
        _pos_int(
            "per_desk_cap",
            "Max alerts emitted per desk per scan, worst-first; the remainder "
            "folds into ONE honest per-desk rollup whose members' watermarks "
            "still advance.",
        ),
        _pos_int(
            "per_watch_cap",
            "Max watchlist_hit alerts per watch per scan, applied BEFORE the "
            "shared per-desk cap.",
        ),
        _unit_float(
            "effective_conf_floor",
            "The verified bar: min(confidence, faithfulness) must clear this "
            "for a finding/contention to be alertable.",
        ),
        _window_hours(
            "finding_window_hours",
            "verified_finding scan window; wider than the cadence so a "
            "critique landing after its finding is still seen.",
        ),
        OptionSpec(
            "baseline_days",
            "int",
            "baseline_deviation: trailing same-desk baseline depth in 24h "
            "buckets.",
            minimum=2,
            maximum=_MAX_WINDOW_DAYS,
        ),
        _nonneg_float(
            "baseline_sigma",
            "baseline_deviation: exceedance threshold in sigmas over the "
            "trailing baseline mean.",
            maximum=100.0,
        ),
        _window_hours(
            "geo_window_hours",
            "geo_convergence: rolling window of geolocated signals binned per "
            "scan.",
        ),
        OptionSpec(
            "geo_min_distinct_families",
            "int",
            "geo_convergence: distinct source families that must converge in "
            "one bin before it fires (diversity is the signal).",
            minimum=2,
            maximum=1000,
        ),
        # -- FRAME-3 steady-state guard (2026-08-29) --------------------------
        _nonneg_float(
            "steady_cooldown_hours",
            "verified_finding: a desk's unchanged-band candidate is "
            "suppressed only within this many hours of the desk's last real "
            "page; 0 disables the cooldown (every unchanged candidate pages).",
            maximum=_MAX_WINDOW_HOURS,
        ),
        _flag(
            "suppress_steady_state",
            "verified_finding: the FRAME-3 guard's master switch. False "
            "reproduces pre-guard behavior exactly — every verified_finding "
            "candidate pages.",
        ),
        # -- D2, the 90-day product wager (2026-08-29) ------------------------
        _nonneg_int(
            "daily_page_budget",
            "Fleet-wide hard cap on ACTUAL pages (fan-outs) per UTC calendar "
            "day, across every trigger class, applied after the FRAME-3 "
            "guard and the per-desk cap. 0 pages nothing.",
            maximum=100_000,
        ),
        _nonneg_int(
            "budget_per_kind_cap",
            "Kind-diversity cap: no single trigger_class may take more than "
            "this many of the day's daily_page_budget slots (cumulative "
            "across scans). Prevents one always-critical class (e.g. "
            "situation_escalation) from starving every other kind; a slot "
            "no other kind can fill stays unused rather than backfilled.",
            maximum=100_000,
        ),
        _flag(
            "contention_flip_enabled",
            "D2 kill list: contention_flip defaults OFF. The scan still "
            "runs and its watermarks still advance as if fired; disabled, "
            "no row is written and nothing pages.",
        ),
        _flag(
            "geo_convergence_enabled",
            "D2 kill list: geo_convergence defaults OFF. Same treatment as "
            "contention_flip_enabled.",
        ),
        # -- #82 coverage floor (coverage_floor class) ------------------------
        # Every knob is a
        # legba.data.analysts.deterministic_handlers._coverage_floor_scan.
        # CoverageFloorConfig field under the `coverage_floor_` prefix, over a
        # LEGBA_COVERAGE_FLOOR_* env default. Calibrated 2026-09-03 against the
        # live fleet: 3 of 32 country targets would flag at these values.
        # Loosening min_slice_share is the fastest way to make it noisy.
        _window_days(
            "coverage_floor_window_days",
            "coverage floor: trailing window the entity census and the slice "
            "denominator share.",
        ),
        _pos_int(
            "coverage_floor_min_signals",
            "coverage floor: distinct signals a foreign-polity cluster needs "
            "before it counts as recurring at all.",
        ),
        _pos_int(
            "coverage_floor_min_days",
            "coverage floor: distinct DAYS the cluster must span — "
            "persistence, so one loud news cycle cannot breach.",
            maximum=_MAX_WINDOW_DAYS,
        ),
        _unit_float(
            "coverage_floor_min_mean_magnitude",
            "coverage floor: mean signals.salience.magnitude over the "
            "cluster. What separates a sports-page recurrence from a war.",
        ),
        _pos_int(
            "coverage_floor_min_high_magnitude_signals",
            "coverage floor: signals in the cluster that must individually "
            "reach coverage_floor_high_magnitude.",
        ),
        _unit_float(
            "coverage_floor_min_slice_share",
            "coverage floor: share of the desk's entity-bearing window slice "
            "the cluster must reach. The precision knob — raise it to silence "
            "the class, 1.0 disables it outright.",
        ),
        _unit_float(
            "coverage_floor_high_magnitude",
            "coverage floor: what counts as an individually consequential "
            "signal.",
        ),
        _unit_float(
            "coverage_floor_min_entity_confidence",
            "coverage floor: NER span confidence floor on payload entities.",
        ),
        _pos_int(
            "coverage_floor_max_clusters_per_target",
            "coverage floor: uncovered polities named in one alert's body, "
            "worst-first; the remainder still ride the payload's cluster "
            "list and still watermark (reported is reported).",
        ),
        _nonneg_float(
            "coverage_floor_min_scan_interval_hours",
            "coverage floor: minimum spacing between the heavy 14-day entity "
            "aggregates. 0 runs it every tick.",
            maximum=_MAX_WINDOW_HOURS,
        ),
        _pos_int(
            "coverage_floor_frame_evidence_min_findings",
            "coverage floor: clause 6 (the SA/Yemen fix) — distinct findings, "
            "of ONE open frame's own cited evidence, whose own authored TITLE "
            "must name the missing polity before that frame clears it. Guards "
            "against a single incidental mention.",
        ),
        # -- GEO ROUTING v2 (routed-elsewhere RECEIPT line) -------------------
        # The one knob of legba.data._geo_routing.GeoRoutingConfig this handler
        # genuinely reads: the coverage floor's `routed_elsewhere` receipt
        # counts at max(coverage_floor_high_magnitude, this), so the number it
        # reports is counted at the same floor the (flag-gated) slice recovery
        # leg would admit at. The family's other three knobs belong to
        # runtime/actor_substrate_slice.py, which has no options channel, and
        # are LEGBA_SLICE_GEO_V2_* env-only by design — declaring them here
        # would be exactly the dead config X-1 exists to refuse.
        _unit_float(
            "slice_geo_v2_min_magnitude",
            "geo routing v2: magnitude floor the routed-elsewhere receipt "
            "counts at. Reported, never gated — nothing branches on it.",
        ),
        # -- S-1 production gauge (production_deficit class) -----------------
        # Every knob is a legba.data.registry.production_gauge.GaugeConfig
        # field under the `gauge_` prefix, so the route and the alert plane
        # cannot be tuned apart. Raising a multiple trades recall for
        # precision; the defaults were calibrated against the live fleet
        # (2026-08-03) so the analyst classes fire on nothing and the source
        # class fires on the documented broken set.
        _window_days(
            "gauge_window_days",
            "production gauge: trailing history depth every baseline (cadence, "
            "runs-per-output, source inter-arrival gaps) is computed over.",
        ),
        _pos_float(
            "gauge_analyst_missed_periods",
            "production gauge: whole cron intervals of analyst silence before "
            "a cadence deficit exists. Deliberately above the liveness "
            "watchdog's 2x edge alert — this is the 'still dead N periods "
            "later' tier.",
            maximum=1000.0,
        ),
        _pos_float(
            "gauge_analyst_min_absence_minutes",
            "production gauge: absolute floor under the cadence test so a "
            "fast-cadence analyst cannot page on a few jittered ticks.",
            maximum=_MAX_WINDOW_DAYS * 24.0 * 60.0,
        ),
        _pos_float(
            "gauge_analyst_drought_multiple",
            "production gauge: multiples of an analyst's OWN runs-per-output "
            "rate before barren runs count as a production drought.",
            maximum=1000.0,
        ),
        _pos_int(
            "gauge_analyst_min_runs_since",
            "production gauge: absolute floor on barren runs before a drought "
            "can be declared.",
        ),
        _pos_int(
            "gauge_analyst_min_producing_runs",
            "production gauge: producing runs needed in the window before an "
            "analyst has a production expectation at all (below it the loop "
            "reads insufficient_history, never a deficit).",
        ),
        _pos_float(
            "gauge_source_gap_multiple",
            "production gauge: multiples of a source's own MAXIMUM observed "
            "inter-arrival gap before silence is a drought. Keyed to the "
            "source's own history so a bursty feed raises its own bar.",
            maximum=1000.0,
        ),
        _pos_float(
            "gauge_source_cadence_multiple",
            "production gauge: floor on the source drought bar expressed in "
            "declared poll intervals — covers a feed whose entire history is "
            "one backfill burst (observed max gap ~0).",
            maximum=10000.0,
        ),
        _pos_float(
            "gauge_source_min_drought_minutes",
            "production gauge: absolute floor — no source pages inside this "
            "much silence however tight its own history.",
            maximum=_MAX_WINDOW_DAYS * 24.0 * 60.0,
        ),
        _pos_int(
            "gauge_source_min_signals_for_gap",
            "production gauge: signals needed in the window before the "
            "inter-arrival gap statistic is trusted.",
        ),
        _pos_int(
            "gauge_source_min_polls_for_silent",
            "production gauge: healthy polls needed before ZERO production "
            "counts as the 'silent' sub-state.",
        ),
        _unit_float(
            "gauge_source_max_error_share",
            "production gauge: above this share of errored polls the "
            "condition is an ERROR (the liveness watchdog's beat), not a "
            "production deficit — gauged, never paged, so one fault is not "
            "reported twice under two names.",
        ),
        _pos_int(
            "gauge_backlog_min_owner_runs",
            "production gauge: owner-analyst runs needed in the window before "
            "'the backlog never drains' is a fair reading (below it, the "
            "resolver's own cadence deficit is the honest attribution).",
        ),
        # -- INTEGRITY loops (R-train 2026-08-05) ------------------------
        _window_days(
            "gauge_judge_window_days",
            "production gauge: trailing window for the LLM-judge availability "
            "read. SHORT by design — a judge outage is acute (26 hours of it "
            "wrote 611 floor-only critiques and dropped fleet mean "
            "faithfulness 0.21 with no alarm), and a three-week denominator "
            "would dilute a full day of silence into a rounding error.",
        ),
        _unit_float(
            "gauge_judge_min_adjudicated_share",
            "production gauge: share of critiques that must carry an "
            "adjudicated (judge_status='llm') verdict. Below 1.0 because "
            "individual judge calls legitimately soft-fail without the "
            "component being down.",
        ),
        _unit_float(
            "gauge_judge_share_tolerance",
            "production gauge: the band beneath the adjudicated-share floor "
            "counting as one severity step. The default puts a TOTAL judge "
            "outage at critical, exactly.",
        ),
        # -- METERING loops (#21/#22, 2026-08-15) -------------------------
        _pos_int(
            "gauge_llm_latency_window_minutes",
            "production gauge: trailing window over the llm_calls receipts "
            "for the primary-plane latency read. Short by design — "
            "saturation is acute, and a day-wide denominator averages a bad "
            "hour into invisibility.",
        ),
        _pos_float(
            "gauge_llm_latency_p95_ceiling_ms",
            "production gauge: p95 call duration (ms) that counts as a "
            "latency deficit on the primary LLM component. Default is HALF "
            "the component's client timeout — the leading edge of the "
            "timeout cliff, and the number that must be green before a "
            "budget raise.",
            maximum=3_600_000.0,
        ),
        _pos_int(
            "gauge_llm_latency_min_calls",
            "production gauge: calls needed in-window before the p95 "
            "statistic is trusted (below it the loop reads "
            "insufficient_history). The truncation leg ignores this floor — "
            "one finish_reason='length' receipt is a defect at any sample "
            "size.",
        ),
        _pos_float(
            "gauge_drift_severity_divisor",
            "production gauge: diverged descriptors per severity step in the "
            "live-vs-tree prompt drift loop. The default makes the first "
            "divergence page, because a live prompt that is not the tree's IS "
            "the analytic method actually running.",
            maximum=1000.0,
        ),
        # -- desk_head_staleness (FRAME-1 §6, 2026-08-20) ------------------
        _pos_float(
            "gauge_staleness_max_head_age_hours",
            "production gauge: age (hours) of the OLDEST head a composition "
            "consumed, above which the desk counts as silent past its expected "
            "fire interval. Default 34h = 2x the units' 11h cooldown + fallback "
            "slack. NOT a freshness SLA — the composition may read old heads "
            "under its admissibility horizon; this is the line past which the "
            "operator should know the desk went quiet.",
            maximum=_MAX_WINDOW_DAYS * 24.0,
        ),
        _pos_float(
            "gauge_staleness_window_hours",
            "production gauge: how far back to look for the composition head "
            "carrying the head-age stamp. Short by design — a composition that "
            "stopped running belongs to the cadence loop, not this one.",
            maximum=_MAX_WINDOW_DAYS * 24.0,
        ),
        _pos_float(
            "gauge_state_drift_severity_divisor",
            "production gauge: deactivation-HAZARD descriptors per severity step "
            "in the live-vs-tree STATE drift loop — descriptors the tree calls "
            "draft/retired that are running live, and that a re-registration "
            "from the repo would therefore take off-line.",
            maximum=1000.0,
        ),
    ),
    # The standalone geo scan is a deprecated no-op stub (the emission folded
    # into alert_trigger_scan); its knobs live on the folded handler above.
    "geo_convergence_scan": (),
    # -- desk statistics -----------------------------------------------------
    "desk_baseline": (
        OptionSpec(
            "baseline_days",
            "int",
            "Trailing baseline depth in 24h buckets (floored at 2 by the "
            "handler regardless).",
            minimum=2,
            maximum=_MAX_WINDOW_DAYS,
        ),
        _nonneg_float(
            "baseline_sigma",
            "Uncertainty-band width in sigmas over the robust trailing mean.",
            maximum=100.0,
        ),
    ),
    "band_calibration_tracker": (
        _pos_int(
            "max_scan_rows",
            "Scorecard rows examined per scan when minting band claims.",
        ),
        _window_days(
            "lookback_days",
            "Window over which resolved claims are aggregated for the "
            "calibration readout.",
        ),
    ),
    "calibration_tracking": (
        OptionSpec(
            "bin_count",
            "int",
            "Reliability-diagram bin count.",
            minimum=2,
            maximum=100,
        ),
        _pos_int("rolling_weeks", "Rolling window (weeks) for drift detection."),
        _nonneg_float(
            "drift_threshold",
            "|drift_z| above this raises the drift alert.",
            maximum=100.0,
        ),
        _window_days("lookback_days", "History window for the calibration pull."),
        _nonneg_int(
            "min_exogenous",
            "Minimum exogenously-resolved samples before a Brier score is "
            "reported at all.",
        ),
        _pos_int(
            "forecast_acute_min_sample",
            "Minimum acute-forecast sample before the segregated pilot Brier "
            "is reported.",
        ),
        _flag(
            "pull_from_substrate",
            "Read resolved predictions from the substrate (off = the caller "
            "supplies rows).",
        ),
        _flag("resolve_predictions", "Run the prediction-resolution leg."),
        _flag("issue_acute_forecasts", "Run the acute-forecast ISSUE leg."),
        _flag("resolve_acute_forecasts", "Run the acute-forecast RESOLVE leg."),
    ),
    "forecast_scoreboard": (
        _unit_float(
            "climatology_shrink_w",
            "Shrinkage weight blending the recent rate toward climatology.",
        ),
        _unit_float(
            "p_epsilon", "Probability clamp keeping p away from 0.0 / 1.0."
        ),
        _unit_float(
            "degeneracy_abstain_share",
            "Share of the p-vector that must be non-degenerate before the "
            "issuer will issue rather than abstain (D9 guard).",
        ),
        _nonneg_int(
            "acute_grace_days",
            "Grace period after the forward window closes before a forecast "
            "is graded.",
        ),
        _window_days(
            "lookback_days", "History window for the acute-forecast pulls."
        ),
    ),
    "unit_correctness_scorer": (
        OptionSpec(
            "units",
            "str_list",
            "Bounded-unit analyst ids to score against the gold labels.",
        ),
        _window_days("lookback_days", "Finding window scored per run."),
    ),
    "scorecard_producer": (
        _unit_float(
            "faith_floor",
            "Faithfulness floor below which a verified unit finding is "
            "demoted out of the band basis.",
        ),
        _unit_float("conf_floor", "Confidence floor for band admissibility."),
        _unit_float(
            "conf_confident",
            "Confidence at/above which a band is reported as confident.",
        ),
        _window_hours("lookback_hours", "Signal/finding window per scorecard."),
    ),
    # -- claims / contention / narratives ------------------------------------
    "claim_watch": (
        _unit_float(
            "match_threshold",
            "Fused (vector + entity + geo) score a signal must reach to bear "
            "on an open question.",
        ),
        _pos_int("signal_cap", "New signals examined per run."),
        _pos_int("question_cap", "Open questions loaded per run."),
        _pos_int("edge_cap", "bearing_edges written per run."),
        _pos_int(
            "flag_cap",
            "review_flags written per run — the budget is shared by consumer "
            "flags and (when armed) the F5 question self-flags.",
        ),
        OptionSpec(
            "question_flags",
            "str",
            "F5 (v4.1.0). A matched question the forward consumption walk "
            "finds NO consumer for writes ONE open SELF-flag (output_id = "
            "founded_on_id = the hypothesis id, reason "
            "new_evidence_bears_on_unconsumed_question) — K-4 R4 measured "
            "output_consumption at 0 rows for ALL 112 watched questions, so "
            "the consumer-only walk left review_flags empty ALL-TIME and the "
            "watcher's detect surface invisible. One open flag per question "
            "(the 0107 partial unique index), shared flag_cap. CHOICE-LOCKED: "
            "'on' / 'off'; ships 'off' — the X-1 byte-identical contract, "
            "armed by the same descriptor PUT that arms the bearing gate.",
            choices=("on", "off"),
        ),
        _nonneg_int(
            "embed_cap", "Question embeddings computed per run (0 disables)."
        ),
        _nonneg_float(
            "max_lag_seconds",
            "Cursor lag above which the run reports itself behind rather than "
            "silently skipping.",
        ),
        _nonneg_float(
            "unembedded_hold_max_age_seconds",
            "How long an unembedded signal is held before the cursor advances "
            "past it.",
        ),
        OptionSpec(
            "meta_question_classes",
            "str_list",
            "Harvest classes excluded from matching (v3.2.0 L1). NOT "
            "choice-locked: the harvest vocabulary can grow ahead of this "
            "catalog; unknown classes simply exclude nothing. Explicit [] "
            "disables the exclusion.",
        ),
        _pos_int(
            "global_df_window",
            "Recent attributed signals sampled for the global entity "
            "document-frequency estimate (v3.2.0 L2 hub damping).",
        ),
        _pos_int(
            "global_df_min_signals",
            "Attributed-signal floor below which the global-df discount is "
            "INERT (a df estimated from too few documents is worse than "
            "none).",
        ),
        OptionSpec(
            "deictic_guard",
            "str",
            "CW-3. Refuse to match a thesis that leans on a referent it does "
            "not carry (\"the incident\", \"the alleged Ukrainian attack\") — "
            "K-4 R3 measured the class at 0.133 because the string every "
            "plane reads does not contain the proposition. Skipped and "
            "counted (skipped_deictic_questions), never down-weighted; the "
            "questions stay open in every other read path. The durable fix is "
            "upstream (open_question_tool inlines the origin finding's title "
            "at write time); this is the backstop for rows written before it. "
            "CHOICE-LOCKED: 'on' (default) / 'off'.",
            choices=("on", "off"),
        ),
        OptionSpec(
            "contention_subject_anchor",
            "str",
            "CW-5. Require a fact_contention question's SUBJECT to be present "
            "in the signal — literally or through a resolved canonical alias "
            "— before the pair can edge. Without it the matcher edges off the "
            "contested VALUE alone: K-4 R3's \"which value of 'located in' "
            "for texas\" matched a SpaceX story with no Texas token in it. "
            "Counted contention_subject_unanchored. CHOICE-LOCKED: 'on' "
            "(default) / 'off'.",
            choices=("on", "off"),
        ),
        _nonneg_float(
            "contention_liveness_days",
            "CW-4. A fact_contention question is only watched while its "
            "dispute is live: not arbiter-collapsed, and carrying a non-junk "
            "value asserted within this many days. DEFAULT 0 = DISABLED, and "
            "deliberately so: replayed over the K-4 R3 gold set the filter "
            "removed 7 correct matches for 8 false ones against a 60% base "
            "false rate, because a group COLLAPSES once the arbiter resolves "
            "the dispute — i.e. downstream of the evidence arriving. Built, "
            "tested and one PUT away for when a liveness signal that actually "
            "separates the two contention populations exists; see "
            "claim_watch_guards for the numbers.",
        ),
        _pos_int(
            "max_questions_per_signal",
            "Distinct questions one signal may edge per run before the "
            "omnibus damper drops the remainder, counted in the receipt "
            "(v3.2.0 L3).",
        ),
        OptionSpec(
            "question_statuses",
            "str_list",
            "hypotheses.status values treated as open questions (the handler "
            "docs call out adding 'active'). NOT choice-locked: unlike "
            "fact_contention, hypotheses.status is an open vocabulary, and the "
            "value is passed as a bound array parameter, never interpolated.",
        ),
        # -- W-B1/W-B2 the BEARING PIPELINE (v3.3.0) ---------------------
        # The handler default is 'off', so declaring these changes NOTHING
        # until an operator sets them: the X-1 byte-identical contract.
        OptionSpec(
            "bearing_gate",
            "str",
            "Post-match semantic gate over would-be bearing edges: a small "
            "self-hosted model is asked whether the signal bears on the "
            "thesis and a NO refuses the edge. CHOICE-LOCKED because the "
            "handler treats anything that is not exactly 'on' as OFF — a "
            "typo'd value must fail the catalog loudly, not silently disable "
            "a filter the operator believes is running. Ships 'off'.",
            choices=("on", "off"),
        ),
        OptionSpec(
            "bearing_gate_ref",
            "str",
            "Stack component id the gate asks (default the idle self-hosted "
            "8B). Resolved through the registry + CredentialVault at run "
            "time, so the endpoint and its basic-auth pair are never in code.",
            pattern=_IDENT_PATTERN,
        ),
        _nonneg_int(
            "bearing_gate_cap",
            "Gate calls per run. Candidates past the budget are STAMPED "
            "'deferred' and written, never dropped — the budget is ours, the "
            "loss must not be the matcher's. 0 leaves the leg on with no "
            "calls (everything stamps 'deferred').",
        ),
        _nonneg_int(
            "bearing_confirm_cap",
            "Core-plane confirm judgments per run over gate-YES edges only. "
            "Sized to bearing_gate_cap since CW-1 made the confirm a DECIDER: "
            "an over-cap pair is un-adjudicated, not merely un-annotated. "
            "0 disables the leg (every gate survivor writes 'unconfirmed').",
        ),
        OptionSpec(
            "bearing_confirm_mode",
            "str",
            "What a confirm verdict does. 'blocking' (default, CW-1) DROPS a "
            "confirm-NO candidate the way a gate-NO is dropped — K-4 R3 "
            "measured confirm-yes 0.667 vs confirm-no 0.085 on the live "
            "gated stream. 'advisory' restores the 3.3.0 stamp-only leg. "
            "CHOICE-LOCKED: a typo must fail the catalog loudly rather than "
            "silently restore a population measured at 0.267. An UNRESOLVED "
            "confirm is never blocked under either mode — it is written and "
            "flagged data.bearing_watch='unconfirmed'.",
            choices=("blocking", "advisory"),
        ),
    ),
    "fact_contention_arbiter": (),
    "narrative_mapper": (
        _nonneg_float(
            "echo_window_hours",
            "Pairwise co-carriage window used to derive echo lag.",
            maximum=float(_MAX_WINDOW_HOURS),
        ),
        _pos_int(
            "min_co_carriage",
            "Minimum co-carriage count before a leader→follower echo edge is "
            "stored.",
        ),
        _pos_int(
            "systematic_floor",
            "Co-carriage count at/above which an edge is labelled systematic.",
        ),
        _unit_float(
            "echo_ratio_floor",
            "Share of co-carriages that must run leader-first before the edge "
            "is directional.",
        ),
        _pos_int("max_narratives", "Contention groups reified per run."),
        OptionSpec(
            "statuses",
            "str_list",
            "fact_contention statuses reified into narratives. Choice-locked "
            "to the table's own CHECK vocabulary (migration 0055) — a value "
            "outside it can never match a row, so rejecting it beats silently "
            "reifying nothing.",
            choices=("contested", "surfaced", "collapsed"),
        ),
    ),
    "source_track_record": (
        _nonneg_float(
            "lag_hours",
            "Circularity guard: contention groups resolved more recently than "
            "this are excluded from the track record.",
            maximum=float(_MAX_WINDOW_HOURS),
        ),
    ),
    # -- facts ---------------------------------------------------------------
    "fact_decay_scan": (
        _pos_int("max_facts", "Open facts walked per readout run."),
        _nonneg_int(
            "top_candidates",
            "Revoke candidates listed in the receipt (0 = counts only).",
        ),
    ),
    "fact_decay": (
        _flag("run_expire", "Run the expiry leg of the legacy mutating sweep."),
        _flag("run_decay", "Run the confidence-decay leg."),
    ),
    "nexus_decay": (),
    # -- graph ---------------------------------------------------------------
    "graph_mining": (
        _flag(
            "augment_from_nexuses",
            "Augment the mined graph with open entity_edges rows.",
        ),
        _flag("augment_from_age", "Augment the mined graph from Apache AGE."),
        _edge_families(
            "Which entity_edges families the mining walks. Default excludes "
            "'cooccurrence' — a co-mention is not a tie, and it was 8,635 of "
            "12,732 open rows, so brokerage was largely measuring which nouns "
            "co-occur in the news. 'reference' IS included: for BROKERAGE an "
            "IGO membership is a genuine conduit."
        ),
    ),
    "structural_balance": (
        _flag(
            "augment_from_nexuses",
            "Augment the signed graph with open entity_edges rows.",
        ),
        _flag("augment_from_age", "Augment the signed graph from Apache AGE."),
        _edge_families(
            "Which entity_edges families the balance ratio counts. Default is "
            "'relation' + 'structural' ONLY: 86% of the open signed edge set "
            "is imported Wikidata country->IGO membership at +1, so counting "
            "'reference' made balance_ratio a statement about UN co-membership "
            "rather than about alignment. Widen it deliberately or not at all."
        ),
    ),
    "proposed_edge_governance": (
        _unit_float(
            "promote_min_confidence",
            "Proposed-edge confidence at/above which the edge promotes to a "
            "nexus.",
        ),
        _unit_float(
            "reject_max_confidence",
            "Proposed-edge confidence at/below which an aged edge is rejected.",
        ),
        _nonneg_int(
            "reject_min_age_days",
            "Minimum age before a thin edge becomes rejectable.",
        ),
        _pos_int("max_promotions_per_run", "Promotion cap per run."),
        _pos_int("max_rejections_per_run", "Rejection cap per run."),
        # K-G2 retention. A SEPARATE age-out from reject_*: that one fires on
        # raw confidence and produced_at, this one on EARNED evidence with
        # staleness measured from the newest backing signal.
        _unit_float(
            "retire_bar",
            "Qualification score below which a stale pending candidate is "
            "retired. Must match the reifier's qualification_bar — a lower "
            "value here would retire candidates the typer still wants.",
        ),
        _pos_int(
            "retire_min_sources",
            "Independent-source floor used by the retirement verdict. Mirrors "
            "the reifier's min_independent_sources.",
            maximum=10,
        ),
        _nonneg_int(
            "retire_stale_days",
            "Days without NEW supporting evidence before a below-bar candidate "
            "retires. A candidate that gains a source restarts its clock. 0 "
            "DISABLES retirement.",
        ),
        _pos_int("max_retirements_per_run", "Retirement cap per run."),
    ),
    # -- entities ------------------------------------------------------------
    "entity_resolution": (
        _pos_int("batch_limit", "Signals resolved per run."),
    ),
    "entity_gc": (
        _flag("run_dormant", "Run the dormant-entity leg."),
        _flag("run_duplicates", "Run the duplicate-profile leg."),
        _flag("run_orphans", "Run the orphan-entity leg."),
        _flag("run_source_pause", "Run the failing-source pause leg."),
        _flag("run_orphan_proposed_edges", "Run the orphan proposed-edge leg."),
        _flag("run_compaction", "Run the profile-compaction leg."),
        _flag("run_source_reprobe", "Run the paused-source reprobe leg."),
    ),
    # -- signal pipeline -----------------------------------------------------
    "anomaly_detection": (
        OptionSpec(
            "bucket_interval",
            "str",
            "time_bucket() width for the volume histogram. Restricted to a "
            "fixed allow-list — the value is interpolated into SQL.",
            choices=(
                "15 minutes",
                "30 minutes",
                "1 hour",
                "2 hours",
                "6 hours",
                "12 hours",
                "1 day",
            ),
        ),
        _window_hours("lookback_hours", "History window for the bucket pull."),
        _nonneg_float(
            "z_threshold",
            "Absolute z-score at/above which a bucket is a rate spike.",
            maximum=100.0,
        ),
        OptionSpec(
            "window_buckets",
            "int",
            "Trailing buckets forming the z-score baseline.",
            minimum=2,
            maximum=100_000,
        ),
        _pos_int(
            "novel_lookback",
            "Trailing buckets defining 'has this entity been seen before'.",
        ),
        _flag(
            "pull_from_substrate",
            "Pull buckets from the substrate (off = caller-supplied rows).",
        ),
        _flag(
            "pull_from_timescale",
            "DEPRECATED alias for pull_from_substrate; read only when the "
            "latter is absent.",
        ),
    ),
    "adversarial_signals": (
        _flag("run_velocity", "Run the velocity/low-quality-burst leg."),
        _flag("run_echo", "Run the echo-cluster leg."),
        _flag("run_provenance", "Run the provenance-collision leg."),
    ),
    "cross_source_dedup": (
        _pos_int("max_groups_per_run", "Dedup groups collapsed per run."),
        _pos_int(
            "max_semantic_candidates",
            "Signals the semantic pass queries per run. The candidate query was "
            "unbounded (~100k rows a run); this is the bound.",
        ),
        _unit_float(
            "semantic_threshold",
            "Cosine similarity at/above which two signals are the same story. "
            "Measured, not tuned — see scripts/measure_dedupe_threshold.py.",
        ),
        OptionSpec(
            "qdrant_collection",
            "str",
            "Vector collection searched for semantic near-duplicates.",
            pattern=_IDENT_PATTERN,
        ),
    ),
    "cross_source_coalesce": (
        _flag(
            "enabled",
            "Master gate — the handler no-ops on cadence until this is true.",
        ),
        _window_hours("window_hours", "Temporal window for coalescing."),
        _unit_float("semantic_threshold", "Cosine similarity gate."),
        _unit_float(
            "title_distance_threshold",
            "Normalized title-distance gate applied alongside the vector gate.",
        ),
        OptionSpec(
            "qdrant_collection",
            "str",
            "Vector collection searched for near-duplicates.",
            pattern=_IDENT_PATTERN,
        ),
        _pos_int("max_signals", "Signals examined per run."),
    ),
    # V3/P1 event-plane knobs — the cohesive sibling keeps the catalog under
    # its module-size ceiling (same split idiom as handler_options_programs).
    **_EVENTS_CATALOG, **_INQUIRY_CATALOG,  # V3 P1/P1b/P4b+H13 event-plane knobs (handler_options_events.py) + Program 5 lane 1's inquiry_yield (handler_options_inquiry.py)
    "corpus_indexer": (_pos_int("batch_limit", "Signals indexed per run."),),
    "corpus_retention": (
        _pos_int("batch_limit", "Tombstoned corpus docs deleted per run."),
    ),
    "signal_embedder": (
        _pos_int("batch_limit", "Signals selected per run."),
        _pos_int("max_embeds", "Embeddings computed per run."),
    ),
    "signal_summarizer": (
        _pos_int("batch_limit", "Signals selected per run."),
        _pos_int("max_summaries", "Summaries generated per run."),
    ),
    "reenrich_ner": (
        _pos_int("max_reenrich", "Signals re-run through NER per backfill run."),
        OptionSpec(
            "translate_languages",
            "str_list",
            "Language codes translated before NER.",
        ),
    ),
    "reenrich_translation": (
        _pos_int("max_translate", "Signals translated per backfill run."),
        OptionSpec(
            "translate_languages",
            "str_list",
            "Language codes eligible for translation.",
        ),
    ),
    # -- findings / lineage --------------------------------------------------
    "finding_supersession": (
        _window_days("lookback_days", "Finding window examined per run."),
        OptionSpec(
            "scope_analyst_id",
            "str",
            "Restrict supersession to one producing analyst.",
            pattern=_IDENT_PATTERN,
        ),
        OptionSpec(
            "cluster_analyst_id",
            "str",
            "Legacy alias for scope_analyst_id.",
            pattern=_IDENT_PATTERN,
        ),
        OptionSpec(
            "topic_fallback",
            "str",
            "Signature fallback used when a finding carries no situation id.",
            pattern=_IDENT_PATTERN,
        ),
    ),
    "composition_lineage_sweep": (
        _window_hours("window_hours", "Composition-root window swept per run."),
    ),
    # The PER-PROGRAM entries — research_measurement (R-D), desk_reference
    # (A-1), correctness_grader (G1), reference_builder (R2) and
    # layer_divergence (Program 6 L2) — each declared beside its own
    # OptionSpecs in handler_options_programs.PROGRAM_CATALOG and spliced in
    # here whole, the **_EVENTS_CATALOG idiom two lines up. Same keys, same
    # specs, same known_option_names answers; what changed is that the NEXT
    # program block costs this size-gated file zero lines.
    **_PROGRAM_CATALOG,
    # D5 standing external audit. Every knob here bounds a COST (heads read,
    # core-plane calls, external searches), so an operator can widen or narrow
    # the daily audit without a code edit — the volume is deliberately tiny and
    # this is where that decision lives. The tail eight specs are the W-9
    # EXTERNAL GRADING WIDTH block (LEGBA_EXTERNAL_GRADING_WIDTH), moved to
    # handler_options_programs.py (module-size gate follow-up) and spliced
    # back on here so the tuple's keys/order are unchanged; see that module's
    # banner for the knobs themselves.
    "standing_auditor": (
        _window_hours(
            "window_hours", "Desk-head recency window the rotation draws from."
        ),
        _pos_int("max_desks", "Desk heads sampled per run (world read is extra)."),
        _pos_int(
            "max_claims_per_head", "Checkable claims lifted from one head."
        ),
        _pos_int(
            "max_claims_total", "Hard cap on claims audited in one run."
        ),
        _pos_int("search_limit", "Results requested per web_search call."),
    ) + _EXTERNAL_GRADING_WIDTH_OPTIONS,
    "indicator_tracker": (
        _window_days("lookback_days", "Run-over-run diff window."),
    ),
    "situation_clustering": (
        _window_days("lookback_days", "Signal window clustered per run."),
        # -- R1-d, the frame-content gauge -----------------------------------
        # Every field of legba.data._frame_content.FrameContentConfig, under
        # the `frame_content_gauge_` prefix, so a retune cannot silently
        # no-op. The gauge counts and names; none of these knobs can move a
        # frame, a name, an alert or a threshold — they move only how often
        # and over how wide a window the instrument reads.
        _window_days(
            "frame_content_gauge_window_days",
            "frame-content gauge: the census / naming window. Matches the "
            "coverage floor's own so the two instruments cannot disagree "
            "about which fortnight they describe.",
        ),
        _window_days(
            "frame_content_gauge_engagement_days",
            "frame-content gauge: the `desks_engaging` window — latest head "
            "per analyst inside it (design §2.2).",
        ),
        _nonneg_float(
            "frame_content_gauge_min_interval_hours",
            "frame-content gauge: minimum spacing between its heavy 14-day "
            "entity aggregates (measured live at 9.4s, which is why the "
            "coverage floor gates the same read). 0 runs it every tick.",
            maximum=_MAX_WINDOW_HOURS,
        ),
    ),
    "thematic_proposal": (),
    "hypothesis_lifecycle": (),
    "collection_gap": (
        _window_days(
            "window_days",
            "Scorecard-card window aggregated into collection requirements.",
        ),
    ),
    "integrity_sweep": (),
    # -- archive / retention -------------------------------------------------
    "evidence_archiver": (
        _window_hours(
            "window_hours", "Finding-recency window for the citation join."
        ),
        _unit_float(
            "verify_floor", "Verified bar a citing finding must clear."
        ),
        _pos_int("fetch_budget", "Candidate signals fetched per run."),
        _pos_int("max_attempts", "Failed-fetch retry cap across runs."),
        _pos_int("max_object_bytes", "Per-object size cap."),
        _pos_int("max_text_chars", "Extracted-text cap stored into the payload."),
        _nonneg_float(
            "per_host_delay_seconds",
            "Politeness delay between same-host fetches.",
            maximum=3600.0,
        ),
        _pos_float("timeout_seconds", "Per-request timeout.", maximum=3600.0),
        _pos_float(
            "run_deadline_seconds",
            "Soft per-run wall-clock stop.",
            maximum=86400.0,
        ),
        OptionSpec(
            "forbid_license_classes",
            "str_list",
            "License classes never archived.",
        ),
        OptionSpec(
            "web_origin_license_gate",
            "str",
            "Posture for a web-origin object whose license is unreviewed.",
            choices=("fail_closed", "inherit"),
        ),
        OptionSpec(
            "unknown_license_gate",
            "str",
            "Posture for ANY object (curated included) whose license is "
            "unset or 'unknown'. 'archive' = the shipped fail-OPEN default; "
            "'fail_closed' = withhold the bytes, keep the metadata. "
            "Recommended fail_closed once your own catalog is classified.",
            choices=("archive", "fail_closed"),
        ),
    ),
    "signals_retention": (
        _nonneg_int(
            "ttl_days",
            "Age above which signals are purged. 0 disables the sweep (the "
            "shipped posture); takes precedence over the env fallback.",
        ),
        _pos_int("batch_limit", "Rows deleted per batch."),
    ),
    "analyst_traces_retention": (
        _nonneg_int(
            "ttl_days",
            "Age above which analyst_traces rows are purged. 0 disables "
            "(the shipped posture); takes precedence over the env fallback. "
            "Keep well above 7 days — the telemetry API aggregates a 7-day "
            "window.",
        ),
        _pos_int("batch_limit", "Rows deleted per batch."),
    ),
}


# ---------------------------------------------------------------------------
# THE KIND CATALOG — analyst ``identity.kind`` → its declared knobs
# ---------------------------------------------------------------------------
#
# X-1 shipped ONE catalog, keyed by deterministic SUB-HANDLER, and the schema
# refused ``method.options`` on every other kind for a good reason: nothing else
# routed through the catalog, so a block anywhere else could only ever be inert,
# and a silent inert block is exactly the dead config X-1 exists to remove.
#
# QW1-B opens a SECOND, deliberately narrow lane: an analyst KIND may declare
# knobs read by the kind's own ``run_method`` (not by a deterministic
# sub-handler). It inherits the whole X-1 contract unchanged — defaults live in
# the code and are never copied here, unknown keys degrade LOUDLY with a receipt,
# values are validated, runtime-owned keys are refused — so the only thing that is
# new is WHERE the knob is read.
#
# The lane stays narrow ON PURPOSE: a kind appears here only when its
# ``run_method`` actually reads ``options[...]``, and the drift guard
# (``tests/data_pkg/test_handler_options_x1.py``) holds every declared name to a
# real call site. A kind absent from this map still cannot carry
# ``method.options`` — the schema refuses it at registration.

#: One focus token: a term (any word/space/punctuation, incl. non-ASCII so an
#: Arabic or Persian keyword is expressible) with an OPTIONAL ``:weight`` suffix.
#: Bounded length so a knob cannot smuggle a paragraph into the ranking scan.
_FOCUS_TOKEN_PATTERN = re.compile(r"^[\w .,'\-/&()]{1,64}(:\d{1,3}(\.\d{1,3})?)?$")

#: One ``judge_sample_always`` member: a finding kind or analyst id — the same
#: lowercase snake_case identifier alphabet both use.
_ANALYST_TOKEN_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# J2 (2026-08-15) — the verify-path JUDGE SAMPLING gate, settable on every
# verify-bearing kind. UNLIKE every other kind knob these are NOT read by the
# kind's own ``run_method``: the actor plane's verify seam reads them off the
# merged run options (``dapr_actors`` → ``actor_critic`` →
# ``provenance.judge_assessability.JudgeSamplingPolicy``), because the judge
# runs AFTER the finding lands, outside run_method. Declared here because this
# catalog is the one operator-facing channel (registration gate + live PUT +
# loud degrade) for descriptor-borne knobs.
#: AMENDMENT 4 (2026-09-07) — the two composition knobs. Both are REGIME
#: switches rather than tuning dials, and both take the house `_coerce` shape:
#: an env var supplies the fleet base and the option below WINS over it, so a
#: rollout can move ONE descriptor and watch it rather than thirty-two at once.
_COMPOSITION_REGIME_OPTION_SPECS: tuple[OptionSpec, ...] = (
    OptionSpec(
        "lead_test_v2",
        "bool",
        "LEAD TEST v2 (env LEGBA_LEAD_TEST_V2 supplies the base; this wins). "
        "ON: the earned-lead count bar is relative to the dimensions the desk "
        "could actually field — max(6, 8 - no_head) — instead of an absolute "
        "8 that a 7-dimension roster can never meet; the earned crown is the "
        "top-cited-mass block instead of unconditionally ordinal 1; and the "
        "co-lead band is anchored on the maximum mass and emitted in mass "
        "order instead of being anchored on ordinal 1 by construction. It "
        "moves lead.kind and lead.block_ordinals ONLY — the block order is "
        "unchanged. OFF is byte-identical to the shipped read, lead.test keys "
        "included.",
    ),
    OptionSpec(
        "rollup_mass_floor",
        "float",
        "Minimum cited_mass a member block must EXCEED to be carried by mass "
        "into a region rollup (env LEGBA_ROLLUP_MASS_FLOOR supplies the base; "
        "this wins). Default 0.0 == carry any block with mass at all, which "
        "is byte-identical to the shipped carry. Documented SAFE BAND is (0, "
        "0.10]: measured over 102 live member-carries, 0.10 moves 28 carries "
        "(27.5%), 0.25 moves 50 (49.0%) and 0.50 moves 70 (68.6%) — above the "
        "band this stops being a noise floor and becomes a different product, "
        "because a member whose every block is under the floor falls back to "
        "ordinal 1 and carries LESS evidence, not more.",
        minimum=0.0,
        maximum=1.0,
    ),
)


_JUDGE_SAMPLING_OPTION_SPECS: tuple[OptionSpec, ...] = (
    OptionSpec(
        "judge_sample_rate",
        "float",
        "Fraction of this analyst's findings the LLM faithfulness judge "
        "grades (the J2 sampling gate). DETERMINISTIC per finding — SHA-256 "
        "of the finding id vs the rate, replayable, no RNG. An unsampled "
        "finding keeps the deterministic floor under the PROVISIONAL "
        "ceiling and publishes judge_status='unsampled' (an honest state, "
        "never an error), with overall_score still a real float. Absent ⇒ "
        "no gate: every finding is judged, exactly as before J2.",
        minimum=0.0,
        maximum=1.0,
    ),
    OptionSpec(
        "judge_sample_always",
        "str_list",
        "Finding kinds and/or analyst ids the judge ALWAYS grades regardless "
        "of judge_sample_rate. Absent ⇒ the code default "
        "(judge_assessability.JUDGE_SAMPLE_ALWAYS_DEFAULT: compositions + "
        "world + journal — meta_findings_synthesizer, "
        "cross_analyst_correlator, situation_tracker, journal_assessor). An "
        "explicit empty list CLEARS the default so a rate can gate "
        "everything.",
        pattern=_ANALYST_TOKEN_PATTERN,
    ),
)

ANALYST_KIND_OPTIONS: dict[str, tuple[OptionSpec, ...]] = {
    "inline_target": (
        OptionSpec(
            "slice_focus",
            "str_list",
            "Per-unit slice RANKING hints: keyword terms, each optionally "
            "weighted as 'term:2.5' (default weight 1.0), matched "
            "case-insensitively against each packed signal's title + body. The "
            "matched rows are re-ORDERED best-first; the row SET is unchanged "
            "(this is never a filter — nothing is hidden from the model or from "
            "derived_from). Absent ⇒ byte-identical recency order.",
            pattern=_FOCUS_TOKEN_PATTERN,
        ),
        OptionSpec(
            "slice_focus_entity_classes",
            "str_list",
            "Per-unit slice RANKING hints over the signal's entity_classes "
            "array (the 9 retained vertex labels: Entity, Location, "
            "Organization, Person, Event, Country, Concept, Corporation, "
            "Software), each optionally weighted as 'Person:2'. Additive with "
            "slice_focus; same re-order-never-filter contract.",
            pattern=_FOCUS_TOKEN_PATTERN,
        ),
        # V3/P2 — THE GRANT. The plan's acceptance step is "the flag on and ONE
        # analyst granted event citations", so the offer is a per-analyst
        # descriptor knob rather than an env flag: a process-wide switch cannot
        # express "escalation only". Read by the SLICE READER
        # (``runtime.actor_substrate_slice``), which is where the grounding
        # gather fires — earlier than run_method, which is why it resolves the
        # descriptor's block itself rather than reading the merged run options.
        _flag(
            "offer_events",
            "Render the OPEN EVENTS desk-grounding block: this desk's live "
            "events (reached through its OPEN situations), each line carrying "
            "the event's 'event:<uuid>' token so the unit can cite the "
            "occurrence's underlying REPORTS (LEGBA_EVENT_CITATIONS expands "
            "the token into ordinary [N] per-signal citations at write time). "
            "Absent/false ⇒ no query, no block, and a rendered prompt "
            "byte-identical to the five-block render. The event's own summary "
            "is never rendered and never evidence.",
        ),
        _pos_int(
            "open_events_limit",
            "Max events in the OPEN EVENTS block (1-20). Absent ⇒ the code "
            "default (unit_grounding.OPEN_EVENTS_CAP = 8). Inert unless "
            "offer_events is true. A unit prompt is budgeted for an ORIENTING "
            "index, not a second evidence slice — hence the 20 ceiling.",
            maximum=20,
        ),
        # 7g-2 — THE SECOND GRANT, on exactly the terms of the first. The
        # HISTORICAL SERIES block reads a curated COLLECTION holding rather
        # than this platform's own memory, so the offer is per-desk: a
        # process-wide switch cannot express "the desks whose history we
        # actually loaded". Read by the SLICE READER for the same reason
        # offer_events is.
        _flag(
            "offer_history",
            "Render the HISTORICAL SERIES desk-grounding block: the curated "
            "historical observations held for this desk (7g's `observations`, "
            "loaded once by the operator and fenced from every live surface). "
            "Each series LINE takes its own [N] and resolves to one stored "
            "observation with its provider file, and each ends with the "
            "stale-tense marker '(historical: valid YYYY..YYYY, recorded "
            "YYYY-MM)' so a past figure can never be graded as a current "
            "claim. Absent/false ⇒ no query, no block, and a rendered prompt "
            "byte-identical to the six-block render. A desk no loaded holding "
            "names renders no block even with the flag on — an absent holding "
            "is not an empty history.",
        ),
        _pos_int(
            "history_series_limit",
            "Max series LINES in the HISTORICAL SERIES block (1-40). Absent ⇒ "
            "the code default (history_grounding.HISTORY_SERIES_CAP = 12). "
            "Inert unless offer_history is true. Each line costs an ordinal, "
            "so this is a bound on the prompt's ORIENTING index, not on the "
            "holding.",
            maximum=40,
        ),
        *_NARRATIVE_SPREAD_BLOCK_OPTIONS,  # narrative_coordination's SPREAD BLOCK knob
        # J2 — the unit findings are the SAMPLED verify population (the tree
        # default rides the unit descriptors at 0.10).
        *_JUDGE_SAMPLING_OPTION_SPECS,
    ),
    # J2 — every OTHER verify-bearing kind may set the same gate. Their kinds
    # sit in JUDGE_SAMPLE_ALWAYS_DEFAULT, so a bare judge_sample_rate on one of
    # these is protected (always judged) until an explicit judge_sample_always
    # clears the membership — the deliberate two-step for sampling a
    # composition. The verify seam reads these, not run_method (banner above).
    # Amendment 4's two composition regime switches ride the SAME kind entry —
    # spliced onto the tail rather than given a second key, which would be a
    # duplicate-key dict literal in which only the last one survived.
    "meta_findings_synthesizer": (
        _JUDGE_SAMPLING_OPTION_SPECS + _COMPOSITION_REGIME_OPTION_SPECS
    ),
    "cross_analyst_correlator": _JUDGE_SAMPLING_OPTION_SPECS,
    "situation_tracker": _JUDGE_SAMPLING_OPTION_SPECS,
    "journal_assessor": _JUDGE_SAMPLING_OPTION_SPECS,
    # Program 5 — the `inquiry` KIND's descriptor-borne mandate (brief /
    # target_scope / pre_pass_module), read by inquiry.run_method at PLAN. No
    # judge-sampling specs: this kind's verify gate rides identity.kind like the
    # journal family's, and its scoring instrument is YIELD (design §4), not the
    # sampled faithfulness population.
    "inquiry": _INQUIRY_KIND_OPTIONS,
    # K-G2. The reifier's throughput and quality dials — the specs themselves
    # live in handler_options_programs (extracted VERBATIM, same key, same
    # order) so this module stayed under its ceiling when the inquiry block
    # above landed.
    "relationship_reifier": _RELATIONSHIP_REIFIER_OPTIONS,
}
