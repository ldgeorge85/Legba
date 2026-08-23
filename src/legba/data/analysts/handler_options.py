# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X-1 — the declared option catalog for deterministic sub-handlers.

**The defect this closes.** Every deterministic handler was written to read
its thresholds out of the run ``options`` mapping
(``options.get("per_desk_cap", DEFAULT_PER_DESK_CAP)`` and ~60 siblings), but
no channel ever fed descriptor-sourced values into that mapping: the runtime
built ``options`` from scratch at fire time (``dapr_actors``) and
``MethodBlock`` was ``extra="forbid"`` with no ``options`` field. Every one of
those knobs was therefore unreachable dead config — the in-source default
always won, and several descriptors DOCUMENTED knobs they could not set.

**The mechanism.** ``method.options`` on the analyst descriptor (see
:class:`legba.data.schemas.analyst.MethodBlock`), merged into the run options
at fire time by the runtime. Descriptor-borne, so it inherits the registry's
versioning + content-hash + audit chain, and — because the runtime reads the
descriptor from its registry DB ROW, not the YAML file — it is live-editable
via ``PUT /api/v1/descriptors/analyst/{id}`` with no code edit, no schema
change and no image rebuild, exactly like the action-pack side's
``ToolSpec.config``.

**The contract, in four parts.**

1. *Defaults are byte-identical.* A descriptor with no ``method.options``
   block contributes NOTHING to the run options mapping — not a key, not a
   sentinel. Every handler's own ``options.get(key, DEFAULT)`` therefore
   resolves to the same in-source constant it always did. This module
   deliberately does **not** record each knob's default value: duplicating
   those constants here would create exactly the unsynchronized-copy problem
   the verify-floor already suffers (four drifting copies of 0.50). The
   handler's own default is the single source of truth; the catalog describes
   only the key's TYPE and admissible RANGE.

2. *Unknown keys degrade LOUDLY.* A key the running code does not declare is
   dropped, logged at WARNING, and noted on the run receipt
   (``analyst_traces.intermediate_steps``) — never silently swallowed, and
   never fatal. Fatal was rejected deliberately: registry rows outlive code,
   so a knob renamed in a later release would otherwise brick activation for
   every descriptor still carrying the old name — a fleet outage in exchange
   for a cosmetic problem.

3. *Values are validated.* A cap must be a positive int, a floor must sit in
   [0, 1], an enum must name a declared choice. A value that fails its
   invariant is dropped with the same loud-degrade treatment, so the handler
   default stands rather than a nonsense threshold taking effect.

4. *Runtime-owned keys are refused.* ``analyst_id`` / ``run_id`` /
   ``target_id`` / ``sub_handler`` and friends are provenance the runtime
   stamps; a descriptor that could overwrite them could forge lineage. They
   are rejected as ``reserved_key`` regardless of the per-handler catalog, as
   is any private ``_``-prefixed key (those are test hooks).

Merge semantics, validated invariants and loud degrade-to-default are lifted
straight from :mod:`legba.data.facts.decay`'s ``LEGBA_FACT_DECAY_CONFIG``
overlay — the one place in the tree that had already solved this problem
well. What the descriptor route adds over that JSON file is per-analyst
scope (two descriptors sharing a sub-handler can differ), versioning, and no
container recreate to change a value.

Adding a knob to a handler? Declare it here in the same commit. An
undeclared knob is not settable, which is the point: the catalog IS the
operator-facing contract, and ``tests/data_pkg/test_handler_options_x1.py``
holds it to the handlers' actual ``options.get`` call sites.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

logger = logging.getLogger(__name__)


__all__ = [
    "ANALYST_KIND_OPTIONS",
    "HANDLER_OPTIONS",
    "OptionReject",
    "OptionResolution",
    "OptionSpec",
    "RESERVED_OPTION_KEYS",
    "known_kind_option_names",
    "known_option_names",
    "resolve_handler_options",
    "resolve_kind_options",
]


# ---------------------------------------------------------------------------
# Reserved keys — runtime-owned, never descriptor-settable
# ---------------------------------------------------------------------------

#: Keys the runtime itself stamps into the run ``options`` mapping at fire
#: time (``dapr_actors``): analyst/target provenance, the run id, the
#: sub-handler route, the per-run agency binding, GATHER wiring, the demoted
#: LLM ref, and the critic-context lookup. A descriptor MUST NOT be able to
#: set any of them — ``analyst_id`` alone would let a descriptor write its
#: side-effect rows under another analyst's name. Rejected as
#: ``reserved_key`` ahead of any per-handler catalog check.
RESERVED_OPTION_KEYS: frozenset[str] = frozenset({
    # identity / provenance
    "analyst_id",
    "analyst_version",
    "run_id",
    "target_id",
    "target_version",
    "owner_tenant",
    # dispatch + kind wiring
    "sub_handler",
    "gather_only",
    "composition",
    "thematic_dimension",
    "contention_groups",
    "source_analyst_ids",
    # agency / GATHER plumbing
    "agency_binding",
    "gather_tool_bindings",
    "gather_web_prompt_fragments",
    "gather_write_prompt_fragments",
    # LLM plane
    "llm_ref",
    "llm_demoted",
    # critic-kind context (resolved from the analyzed analyst's descriptor)
    "analyzed_output_id",
    "analyzed_analyst_id",
    "analyzed_model",
    "allow_self_correlated",
    "rubric",
})


# ---------------------------------------------------------------------------
# Spec shape
# ---------------------------------------------------------------------------

OptionKind = Literal["int", "float", "bool", "str", "str_list"]

#: Guards a string option that reaches an identifier position (a Qdrant
#: collection name). Conservative on purpose.
_IDENT_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


@dataclass(frozen=True)
class OptionSpec:
    """One declared, operator-settable knob on one deterministic handler.

    Carries the key's TYPE and admissible RANGE — never its default. The
    handler's own ``options.get(name, DEFAULT)`` remains the sole source of
    truth for the default, so an absent option is byte-identical to today
    by construction and no constant is copied twice.
    """

    name: str
    kind: OptionKind
    doc: str
    minimum: float | None = None
    maximum: float | None = None
    #: Inclusive-lower by default; set False for a strict ``> minimum`` bound
    #: (e.g. a timeout that must be positive, not merely non-negative).
    minimum_inclusive: bool = True
    choices: tuple[str, ...] | None = None
    #: Compiled guard for ``str`` kinds that reach an identifier position.
    pattern: re.Pattern[str] | None = None

    def validate(self, value: Any) -> tuple[bool, Any, str]:
        """Return ``(ok, coerced_value, cause)``. ``cause`` is "" when ok."""
        if self.kind == "bool":
            if not isinstance(value, bool):
                return False, None, "expected bool"
            return True, value, ""

        if self.kind == "int":
            # bool is an int subclass in Python — refuse it explicitly so a
            # `true` in YAML can never become a cap of 1.
            if isinstance(value, bool) or not isinstance(value, int):
                return False, None, "expected int"
            return self._check_range(float(value), value)

        if self.kind == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False, None, "expected number"
            return self._check_range(float(value), float(value))

        if self.kind == "str":
            if not isinstance(value, str) or not value.strip():
                return False, None, "expected non-empty string"
            if self.choices is not None and value not in self.choices:
                return False, None, f"not one of {list(self.choices)}"
            if self.pattern is not None and not self.pattern.match(value):
                return False, None, "does not match the allowed pattern"
            return True, value, ""

        if self.kind == "str_list":
            if not isinstance(value, (list, tuple)):
                return False, None, "expected a list of strings"
            out: list[str] = []
            for item in value:
                if not isinstance(item, str) or not item.strip():
                    return False, None, "expected a list of non-empty strings"
                if self.choices is not None and item not in self.choices:
                    return False, None, f"'{item}' not one of {list(self.choices)}"
                # A ``pattern`` binds EVERY member, exactly as it binds the whole
                # value on a scalar ``str`` — otherwise a list-shaped knob would
                # be the one place a declared shape guard silently does nothing.
                if self.pattern is not None and not self.pattern.match(item):
                    return False, None, f"'{item}' does not match the allowed pattern"
                out.append(item)
            return True, out, ""

        return False, None, f"unsupported option kind {self.kind!r}"

    def _check_range(self, as_float: float, coerced: Any) -> tuple[bool, Any, str]:
        if self.minimum is not None:
            if self.minimum_inclusive:
                if as_float < self.minimum:
                    return False, None, f"must be >= {self.minimum}"
            elif as_float <= self.minimum:
                return False, None, f"must be > {self.minimum}"
        if self.maximum is not None and as_float > self.maximum:
            return False, None, f"must be <= {self.maximum}"
        return True, coerced, ""


@dataclass(frozen=True)
class OptionReject:
    """One dropped option key + why. Rides the run receipt verbatim."""

    key: str
    cause: str  # unknown_key | reserved_key | private_key | invalid_value |
    #             unknown_handler
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "cause": self.cause, "detail": self.detail}


@dataclass(frozen=True)
class OptionResolution:
    """Outcome of validating one descriptor's ``method.options`` block."""

    accepted: dict[str, Any]
    rejected: tuple[OptionReject, ...]

    @property
    def degraded(self) -> bool:
        return bool(self.rejected)


# ---------------------------------------------------------------------------
# Shared spec constructors (kept terse — the catalog below is long)
# ---------------------------------------------------------------------------


def _pos_int(name: str, doc: str, *, maximum: float | None = None) -> OptionSpec:
    """A cap/limit: an int >= 1."""
    return OptionSpec(name, "int", doc, minimum=1, maximum=maximum)


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


#: A year of hours — the ceiling on every "window/lookback in hours" knob, so
#: a fat-fingered value cannot turn a bounded scan into a full-table walk.
_MAX_WINDOW_HOURS = 8760
#: Ten years of days, same rationale.
_MAX_WINDOW_DAYS = 3650


def _window_hours(name: str, doc: str) -> OptionSpec:
    return OptionSpec(name, "int", doc, minimum=1, maximum=_MAX_WINDOW_HOURS)


def _window_days(name: str, doc: str) -> OptionSpec:
    return OptionSpec(name, "int", doc, minimum=1, maximum=_MAX_WINDOW_DAYS)


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
    "indicator_tracker": (
        _window_days("lookback_days", "Run-over-run diff window."),
    ),
    "situation_clustering": (
        _window_days("lookback_days", "Signal window clustered per run."),
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
        # J2 — the unit findings are the SAMPLED verify population (the tree
        # default rides the unit descriptors at 0.10).
        *_JUDGE_SAMPLING_OPTION_SPECS,
    ),
    # J2 — every OTHER verify-bearing kind may set the same gate. Their kinds
    # sit in JUDGE_SAMPLE_ALWAYS_DEFAULT, so a bare judge_sample_rate on one of
    # these is protected (always judged) until an explicit judge_sample_always
    # clears the membership — the deliberate two-step for sampling a
    # composition. The verify seam reads these, not run_method (banner above).
    "meta_findings_synthesizer": _JUDGE_SAMPLING_OPTION_SPECS,
    "cross_analyst_correlator": _JUDGE_SAMPLING_OPTION_SPECS,
    "situation_tracker": _JUDGE_SAMPLING_OPTION_SPECS,
    "journal_assessor": _JUDGE_SAMPLING_OPTION_SPECS,
    # K-G2. The reifier's throughput and quality dials. THROUGHPUT is
    # max_candidates × batch_size; QUALITY is qualification_bar ×
    # min_independent_sources. They are separate levers on purpose — the bar
    # controls WHICH edges may enter the graph, the cap controls HOW MANY get
    # typed, and the bake-off is explicit that the bar is not a yield optimiser
    # and must not be sold as one (docs/TYPING_BAKEOFF_2026-08-03.md §6.5).
    "relationship_reifier": (
        _pos_int(
            "max_candidates",
            "Candidates typed per run (the cadence is twice daily, so the daily "
            "figure is 2x this). Bounds the per-run LLM spend regardless of how "
            "deep the qualifying queue is. See the arithmetic in "
            "descriptors/analyst_relationship_reifier.yaml.",
            maximum=5000,
        ),
        _pos_int(
            "batch_size",
            "Candidates per LLM typing call. 12 is MEASURED (17/17 clean calls "
            "over 200 candidates, zero truncation) and cuts prompt tokens per "
            "candidate from 1,462 to 297. 1 restores the one-call-per-candidate "
            "shape. Above 24 is unevidenced and showed a possible judgement "
            "shift.",
            maximum=40,
        ),
        OptionSpec(
            "qualification_bar",
            "float",
            "Weighted qualification score a candidate must clear to earn a "
            "typing call. 0.42 is the recommended setting (~12,000 qualifying "
            "of ~176,000 pending). LOWERING it widens the queue but can never "
            "re-admit single-sourced candidates — min_independent_sources is a "
            "separate hard floor for exactly that reason.",
            minimum=0.0,
            maximum=1.0,
        ),
        _pos_int(
            "min_independent_sources",
            "Hard floor on distinct INDEPENDENT sources behind a candidate, "
            "counted after collapsing syndicated content. Not expressible as a "
            "weight: a single-sourced pair with huge salience would otherwise "
            "buy its way in. 92.1% of the live pending pool fails this floor, "
            "and that is the sludge the graph exists to exclude.",
            maximum=10,
        ),
    ),
}


def known_option_names(sub_handler: str) -> tuple[str, ...]:
    """Declared option names for ``sub_handler`` (empty for an unknown one)."""
    return tuple(s.name for s in HANDLER_OPTIONS.get(sub_handler, ()))


def known_kind_option_names(kind: str) -> tuple[str, ...]:
    """Declared option names for analyst ``kind`` (empty for an unknown one)."""
    return tuple(s.name for s in ANALYST_KIND_OPTIONS.get(kind, ()))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_handler_options(
    sub_handler: str | None,
    raw: Mapping[str, Any] | None,
    *,
    log_context: str = "",
) -> OptionResolution:
    """Validate a descriptor's ``method.options`` against the live catalog.

    Returns the accepted subset plus a rejection list. NEVER raises and never
    partially applies a bad value: a key either lands intact or is dropped
    whole. An empty/absent ``raw`` yields an empty resolution, which is what
    makes an options-less descriptor byte-identical to today.

    ``log_context`` (typically ``analyst_id@version``) is folded into the
    warning lines so an operator can find the offending descriptor.
    """
    return _resolve_against(
        HANDLER_OPTIONS.get(sub_handler or ""),
        raw,
        label=sub_handler,
        label_noun="sub_handler",
        unknown_cause="unknown_handler",
        log_context=log_context,
    )


def resolve_kind_options(
    kind: str | None,
    raw: Mapping[str, Any] | None,
    *,
    log_context: str = "",
) -> OptionResolution:
    """Validate a descriptor's ``method.options`` against the KIND catalog.

    The :data:`ANALYST_KIND_OPTIONS` twin of :func:`resolve_handler_options`, for
    knobs an analyst KIND's own ``run_method`` reads rather than a deterministic
    sub-handler. Identical contract in every respect — same reserved-key refusal,
    same loud degrade, same never-raises, same "an absent block is byte-identical
    to today" — differing only in which catalog the names are checked against.
    """
    return _resolve_against(
        ANALYST_KIND_OPTIONS.get(kind or ""),
        raw,
        label=kind,
        label_noun="kind",
        unknown_cause="unknown_kind",
        log_context=log_context,
    )


def _resolve_against(
    specs: tuple[OptionSpec, ...] | None,
    raw: Mapping[str, Any] | None,
    *,
    label: str | None,
    label_noun: str,
    unknown_cause: str,
    log_context: str,
) -> OptionResolution:
    """The ONE validation traversal both catalogs share.

    Extracted rather than copied: a per-catalog copy of the reserved-key refusal,
    the private-key refusal, the value validation and the loud-degrade logging is
    exactly the unsynchronized-copy problem this module's own docstring warns
    about.
    """
    if not raw:
        return OptionResolution(accepted={}, rejected=())

    accepted: dict[str, Any] = {}
    rejected: list[OptionReject] = []

    if specs is None:
        # A route this build does not register. Every key is unusable, but the
        # run still proceeds on pure defaults — the dispatcher itself will fail
        # loudly if the route is genuinely dead.
        for key in raw:
            rejected.append(
                OptionReject(
                    key=str(key),
                    cause=unknown_cause,
                    detail=(
                        f"{label_noun} {label!r} declares no option "
                        "catalog in this build"
                    ),
                )
            )
        _log(rejected, label, log_context)
        return OptionResolution(accepted={}, rejected=tuple(rejected))

    by_name = {s.name: s for s in specs}
    for key, value in raw.items():
        name = str(key)
        if name.startswith("_"):
            rejected.append(
                OptionReject(
                    name,
                    "private_key",
                    "keys starting with '_' are private runtime/test hooks",
                )
            )
            continue
        if name in RESERVED_OPTION_KEYS:
            rejected.append(
                OptionReject(
                    name,
                    "reserved_key",
                    "stamped by the runtime (identity/provenance/dispatch); a "
                    "descriptor may not override it",
                )
            )
            continue
        spec = by_name.get(name)
        if spec is None:
            rejected.append(
                OptionReject(
                    name,
                    "unknown_key",
                    (
                        f"{label} declares no such option; known: "
                        f"{sorted(by_name)}"
                    ),
                )
            )
            continue
        ok, coerced, cause = spec.validate(value)
        if not ok:
            rejected.append(
                OptionReject(name, "invalid_value", f"{cause} (got {value!r})")
            )
            continue
        accepted[name] = coerced

    _log(rejected, label, log_context)
    return OptionResolution(accepted=accepted, rejected=tuple(rejected))


def _log(
    rejected: list[OptionReject], route: str | None, log_context: str
) -> None:
    """One WARNING per dropped key — loud degrade, never silent, never fatal."""
    for rej in rejected:
        logger.warning(
            "handler_options.rejected route=%s descriptor=%s key=%s "
            "cause=%s detail=%s — the declared default stands",
            route,
            log_context or "?",
            rej.key,
            rej.cause,
            rej.detail,
        )
