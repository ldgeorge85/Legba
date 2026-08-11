# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-1 — the expected-vs-actual PRODUCTION gauge.

Liveness instrumentation exists in this engine; production instrumentation
does not. The distinction is the whole point of this module, and it is not
academic — it is the difference between the twelve entries on the
ENGINE_REVIEW_2026-08-02 §1 census and an engine that notices its own limbs
dying. A liveness check asks *is it running*. Every one of those twelve
answered YES while producing NOTHING:

  * five ``rsshub.apnews.*`` feeds polled **130 times each over six days**,
    every poll ``outcome='success'``, ``health_state='healthy'``, and wrote
    **zero** signals after their first day. Zero alerts ever fired for them.
  * ``forecast_scoreboard`` ran daily, receipted daily, and resolved **0 of
    38** forecasts — 19 of them overdue since 2026-07-06.
  * the journal died for 38h behind a moved actor id; the aggregate pipeline
    never went quiet, so nothing that watches aggregates noticed.

This module is the READER that turns those absences into a number. It writes
nothing, requires no new instrumentation from any producer, and derives every
expectation from data that already exists: descriptor cadence declarations,
``analyst_traces``, ``analyst_outputs``, ``source_poll_outcomes`` and
``signals``.

Why the existing watchdog could not see it
------------------------------------------
:mod:`legba.runtime.liveness_watchdog` is a good watchdog and this module does
not replace it. It is blind to the production class for two mechanical
reasons, both worth stating because they are the design constraints here:

1. **It reads a timestamp that lies.** Its source-staleness leg keys on
   ``max(signals.fetched_at)``, and ``fetched_at`` is bumped on every poll
   whether or not the content changed (roadmap B-1). For the frozen AP feeds
   ``max(fetched_at)`` was *seconds* old while ``max(created_at)`` — the row's
   actual birth — was six days old. The freshness read was structurally
   incapable of seeing the freeze.
2. **Its empty-streak leg counts only ``outcome='empty'`` rows.** The AP polls
   recorded ``outcome='success'`` (the classifier promotes any poll that
   parsed entries, even when the since-filter drops all of them), so the
   streak broke on its very first row, forever.

This gauge therefore keys production on ``signals.created_at`` — which cannot
be back-dated by a handler and cannot be bumped by a no-op poll — and counts
*production events*, never poll outcomes.

The four loop classes
---------------------
Three derive their expectations entirely from descriptors and observed
history, so a newly-activated analyst or source is gauged from its first
cadence tick with no code change anywhere. That auto-covering property is the
reason the expectation model is data-driven rather than a registry of
components, which would rot the first time somebody added a desk.

``analyst_cadence``
    *Expected*: an active analyst descriptor's ``cadence.fallback_schedule``
    cron says when it should fire; :func:`~.source_freshness.cadence_interval_minutes`
    turns that into the maximum fire-to-fire gap (the honest reading of
    ``47 5-23/6 * * *`` is 360min, not the naive field step).
    *Actual*: ``max(analyst_traces.run_started_at)``.
    *Deficit*: silence past ``analyst_missed_periods`` (default 3) whole
    intervals. Deliberately SLOWER than the watchdog's 2x edge alert: this
    tier means "still dead three periods later, and nobody acted", it runs in
    a different process from the watchdog (so a dead watchdog cannot hide it),
    and it is durable rather than edge-only.

``analyst_production``
    *Expected*: the analyst's OWN observed rate — runs-per-producing-run over
    the baseline window.
    *Actual*: runs since its last producing run.
    *Deficit*: ``analyst_drought_multiple`` (default 6) times its own rate.
    A "producing run" is ``cardinality(output_row_refs) > 0`` OR an
    ``analyst_outputs`` row carrying the run id — a union that covers all
    twelve OutputKinds and every destination table (journal rows land in
    ``journal_entries``, scorecards side-write into ``analyst_outputs``) with
    NO per-kind table map to maintain.
    An analyst that produced nothing across the whole window is
    ``trace_only_by_observation`` — not a deficit. That single rule exempts
    every side-effect sweep in the fleet (``cross_source_dedup``,
    ``integrity_sweep``, ``alert_trigger_scan`` …) without naming one of them.

``source_production``
    *Expected*: the source's OWN observed inter-arrival history — the MAXIMUM
    gap between consecutive ``signals.created_at`` values in the window,
    which is what makes a genuinely bursty feed (USGS earthquakes, NASA
    disaster events) un-false-alarmable: its historical max gap is already
    large, so the bar rises with it. Floored by the declared poll cadence so a
    feed whose entire history is one backfill burst (max gap ~0) is still
    gauged.
    *Actual*: minutes since ``max(signals.created_at)``.
    *Deficit*: past ``max(gap_multiple x max_observed_gap,
    cadence_multiple x poll_interval, min_drought_minutes)``.
    A source with too few signals to have a gap history falls to the
    ``silent`` sub-state: zero production across the window while polling
    healthily N times.

``backlog_drain``
    The one class that is NOT auto-covering, and the honest reason is that no
    descriptor in this engine declares what "overdue work" means for its
    table. "A forecast whose window closed more than a day ago and carries no
    ``resolved_outcome``" is table-specific semantics that lives nowhere else,
    so it lives in :data:`BACKLOG_DRAINS` here — a small declaration whose SQL
    is schema-checked by a test, so it fails loud rather than rotting quiet.
    *Deficit*: overdue rows exist AND the owning analyst ran AND nothing was
    resolved in the window. That is exactly the forecast-resolution shape:
    green liveness, green receipts, zero product, for the component's entire
    life.

Precision over recall, deliberately
-----------------------------------
False pages erode the instrument; this one only earns its place if a page
means something. Every bar above is a multiple of the loop's own history, not
a global constant, and each class carries an absolute floor beneath the
statistical test so a quiet loop with sigma ~0 cannot page on noise. The
alert plane additionally takes only ``medium`` and worse
(:data:`ALERT_MIN_SEVERITY`); ``info``/``low`` deficits surface on the route
and stay off the operator's phone. Measured against the live fleet on
2026-08-03 the analyst classes fire on NOTHING and the source class fires on
the documented broken set — which is the calibration this module was tuned to.

Quiet-by-design is a first-class answer
---------------------------------------
Not-gauged is never rendered as healthy. A loop with no honest expectation
reads ``ungauged`` with a stated :attr:`LoopGauge.quiet_reason` —
``not_active`` (draft/configured/paused/retired), ``no_declared_cadence``
(the on-demand consult kinds), ``gather_only`` (an analyst that opts out of
cadence-driven substrate consumption), ``trace_only_by_observation``,
``activation_grace``, ``insufficient_history``. The route publishes those
counts beside the deficits, because "we cannot say" and "it is fine" are
different statements and blurring them is how the twelve got missed.

One implementation, two readers (the :mod:`.source_freshness` precedent):
:mod:`legba.data.registry.production_gauge_api` serves it, and the
``production_deficit`` trigger class in
:mod:`legba.data.analysts.deterministic_handlers._production_deficit_scan`
pages on it. Neither owns the judgment.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping, Optional, Sequence

from .backlog_drains import BACKLOG_DRAINS, BacklogDrain  # noqa: F401 — re-export
from .source_freshness import cadence_interval_minutes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Loop-class identifiers. Also the first half of a gauge row's watermark key.
LOOP_ANALYST_CADENCE = "analyst_cadence"
LOOP_ANALYST_PRODUCTION = "analyst_production"
LOOP_SOURCE_PRODUCTION = "source_production"
LOOP_BACKLOG_DRAIN = "backlog_drain"

#: R-train 2026-08-05 — the INTEGRITY classes. Same contract, different question:
#: the four above ask whether a loop PRODUCES, these ask whether what it produces
#: is still what we think it is. Defined in ``production_gauge_integrity``; named
#: here so ``LOOP_CLASSES`` stays the one enumeration.
LOOP_JUDGE_AVAILABILITY = "judge_availability"
LOOP_DESCRIPTOR_PROMPT_DRIFT = "descriptor_prompt_drift"
LOOP_DESCRIPTOR_STATE_DRIFT = "descriptor_state_drift"

LOOP_CLASSES: tuple[str, ...] = (
    LOOP_ANALYST_CADENCE,
    LOOP_ANALYST_PRODUCTION,
    LOOP_SOURCE_PRODUCTION,
    LOOP_BACKLOG_DRAIN,
    LOOP_JUDGE_AVAILABILITY,
    LOOP_DESCRIPTOR_PROMPT_DRIFT,
    LOOP_DESCRIPTOR_STATE_DRIFT,
)

GaugeState = Literal["ok", "deficit", "ungauged"]

#: Severity ladder shared with the alert plane (``AlertPayload.severity``).
SEVERITY_RANK: dict[str, int] = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

#: Deficits below this severity surface on the route but never page. The
#: precision knob: an ``info``/``low`` deficit is a number worth reading, not
#: an interruption worth having.
ALERT_MIN_SEVERITY = "medium"

#: Junk template/autowire source descriptors, excluded from the gauge exactly
#: as ``/v3/system/source-firing`` and the source-quality ledger exclude them
#: — they are registration scaffolding, not sources, and gauging them would
#: manufacture permanent deficits. Kept in lockstep with
#: ``source_quality_api.JUNK_DESCRIPTOR_PREFIXES`` by a test.
JUNK_SOURCE_PREFIXES: tuple[str, ...] = (
    "src_autowire_p13_",
    "src_locked_p13_",
    "src_template_p13_",
    "src_tmpl_aw_",
    "src_tmpl_ds_",
    "src_disc_",
)

# Quiet-by-design reasons (the ``ungauged`` vocabulary).
QUIET_NOT_ACTIVE = "not_active"
QUIET_NO_CADENCE = "no_declared_cadence"
QUIET_UNPARSABLE_CADENCE = "unparsable_cadence"
QUIET_GATHER_ONLY = "gather_only"
QUIET_TRACE_ONLY = "trace_only_by_observation"
QUIET_NEVER_RAN = "never_ran_within_window"
QUIET_ACTIVATION_GRACE = "activation_grace"
QUIET_INSUFFICIENT_HISTORY = "insufficient_history"
QUIET_NO_BACKLOG = "no_overdue_work"
QUIET_OWNER_NOT_RUNNING = "owner_not_running"
QUIET_POLLING_ERRORS = "polling_errors"

#: Defensive bounds on a single gauge read.
_MAX_ANALYSTS = 500
_MAX_SOURCES = 2_000

_MINUTE = 60.0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GaugeConfig:
    """Every threshold, in one overridable place.

    Defaults are calibrated against the live fleet (2026-08-03): the analyst
    classes fire on nothing, the source class fires on the documented broken
    set. Raising a multiple trades recall for precision; each is exposed as an
    ``alert_trigger_scan`` handler option so the operator can retune without a
    deploy.
    """

    #: Trailing history depth every baseline is computed over.
    window_days: int = 21

    # -- analyst_cadence ---------------------------------------------------
    #: Whole cron intervals of silence before a cadence deficit exists.
    #: Deliberately above the liveness watchdog's 2x edge alert.
    analyst_missed_periods: float = 3.0
    #: Absolute floor beneath the statistical test — a fast-cadence analyst
    #: cannot page on a few missed ticks (jitter, a cooldown landing late).
    analyst_min_absence_minutes: float = 180.0

    # -- analyst_production ------------------------------------------------
    #: Multiples of the analyst's OWN runs-per-output rate before a drought.
    analyst_drought_multiple: float = 6.0
    #: Absolute floor: never call it a drought inside this many runs.
    analyst_min_runs_since: int = 5
    #: Baseline depth required before a production expectation exists at all.
    analyst_min_producing_runs: int = 3

    # -- source_production -------------------------------------------------
    #: Multiples of the source's own MAXIMUM observed inter-arrival gap.
    source_gap_multiple: float = 4.0
    #: Floor expressed in declared poll intervals — covers a source whose
    #: whole history is one backfill burst (observed max gap ~0).
    source_cadence_multiple: float = 24.0
    #: Absolute floor: nothing pages inside a day of silence.
    source_min_drought_minutes: float = 1_440.0
    #: Signals needed before the gap statistic is trustworthy.
    source_min_signals_for_gap: int = 4
    #: Healthy polls needed before ZERO production counts as ``silent``.
    source_min_polls_for_silent: int = 24
    #: Above this share of errored polls the condition is an ERROR (the
    #: watchdog's beat), not a production deficit — gauged, never paged.
    source_max_error_share: float = 0.5

    # -- backlog_drain -----------------------------------------------------
    #: Owner runs needed in the window before "it never drains" is fair.
    backlog_min_owner_runs: int = 3

    # -- judge_availability (R-train 2026-08-05) ---------------------------
    #: Trailing window for the adjudicated-share read. SHORTER than the 21-day
    #: production window on purpose: a judge outage is an acute condition that
    #: must page within hours, and a three-week denominator would dilute a full
    #: day of silence into a rounding error. The 26-hour outage produced 611
    #: floor-only critiques; at 2 days it reads as a total deficit, at 21 days it
    #: reads as noise.
    judge_window_days: int = 2
    #: Share of critiques that must carry an ADJUDICATED (judge_status='llm')
    #: verdict. Below 1.0 because individual judge calls legitimately soft-fail
    #: (a truncated response, one 5xx) without the component being down.
    judge_min_adjudicated_share: float = 0.80
    #: The band beneath the floor that counts as one severity step. 0.20 puts a
    #: TOTAL outage (share 0.0) at ratio 4.0 = critical, exactly.
    judge_share_tolerance: float = 0.20

    # -- descriptor_prompt_drift (R-train 2026-08-05) ----------------------
    #: Diverged descriptors per severity step. 1.0 means the first divergence is
    #: already ``medium`` (it pages) and four are ``critical`` — a fleet-wide
    #: mismatch means a whole PUT run never landed.
    drift_severity_divisor: float = 1.0
    #: Deactivation-HAZARD descriptors per severity step in the state-drift loop
    #: (tree says draft/retired, live head is running). Deliberately blunt: the
    #: live registry carries 76 such rows today, 68 of them active, so this loop
    #: opens at critical and SEEDS without paging under the 0091 contract — then
    #: stays silent until the condition worsens, which is exactly the S-1 shape
    #: for a large standing debt.
    state_drift_severity_divisor: float = 4.0

    @classmethod
    def from_options(cls, options: Mapping[str, Any] | None) -> "GaugeConfig":
        """Build from a handler-option mapping, ignoring unknown keys.

        Values that will not coerce keep their default rather than raising —
        a mistyped knob must not take the whole gauge offline.
        """
        if not options:
            return cls()
        kwargs: dict[str, Any] = {}
        for f, default in _CONFIG_FIELDS.items():
            if f not in options:
                continue
            raw = options[f]
            try:
                kwargs[f] = type(default)(raw)
            except (TypeError, ValueError):
                logger.info(
                    "production_gauge.bad_option name=%s value=%r — keeping "
                    "the default %r",
                    f,
                    raw,
                    default,
                )
        return cls(**kwargs)


_CONFIG_FIELDS: dict[str, Any] = {
    "window_days": 21,
    "analyst_missed_periods": 3.0,
    "analyst_min_absence_minutes": 180.0,
    "analyst_drought_multiple": 6.0,
    "analyst_min_runs_since": 5,
    "analyst_min_producing_runs": 3,
    "source_gap_multiple": 4.0,
    "source_cadence_multiple": 24.0,
    "source_min_drought_minutes": 1_440.0,
    "source_min_signals_for_gap": 4,
    "source_min_polls_for_silent": 24,
    "source_max_error_share": 0.5,
    "backlog_min_owner_runs": 3,
    "judge_window_days": 2,
    "judge_min_adjudicated_share": 0.80,
    "judge_share_tolerance": 0.20,
    "drift_severity_divisor": 1.0,
    "state_drift_severity_divisor": 4.0,
}




# ---------------------------------------------------------------------------
# The gauge row
# ---------------------------------------------------------------------------


@dataclass
class LoopGauge:
    """One producing loop's expected-vs-actual verdict.

    ``ratio`` is the honest headline number: observed absence divided by the
    bar that absence had to clear. Below 1.0 the loop is ``ok``; the severity
    ramp reads off multiples above it. It is ``None`` exactly when the loop is
    ``ungauged`` — a reader can never mistake "no expectation" for 0.0.
    """

    loop_class: str
    loop_id: str
    label: str
    state: GaugeState
    severity: str = "info"
    ratio: Optional[float] = None
    expected: str = ""
    actual: str = ""
    quiet_reason: Optional[str] = None
    last_production_at: Optional[datetime] = None
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """The stable identity used for watermarking and route sorting."""
        return f"{self.loop_class}:{self.loop_id}"

    @property
    def rank(self) -> int:
        return SEVERITY_RANK.get(self.severity, 0)

    @property
    def pages(self) -> bool:
        """Does this row clear the alert-plane floor?"""
        return self.state == "deficit" and self.rank >= SEVERITY_RANK[
            ALERT_MIN_SEVERITY
        ]


@dataclass
class GaugeReport:
    """A whole-engine read: every gauged loop plus the roll-up counts."""

    generated_at: datetime
    window_days: int
    loops: list[LoopGauge] = field(default_factory=list)

    @property
    def deficits(self) -> list[LoopGauge]:
        return [g for g in self.loops if g.state == "deficit"]

    def totals(self) -> dict[str, Any]:
        by_sev: dict[str, int] = {}
        by_class: dict[str, dict[str, int]] = {}
        for g in self.loops:
            cls = by_class.setdefault(
                g.loop_class, {"gauged": 0, "ok": 0, "deficit": 0, "ungauged": 0}
            )
            cls[g.state] = cls.get(g.state, 0) + 1
            if g.state != "ungauged":
                cls["gauged"] += 1
            if g.state == "deficit":
                by_sev[g.severity] = by_sev.get(g.severity, 0) + 1
        return {
            "loops": len(self.loops),
            "gauged": sum(1 for g in self.loops if g.state != "ungauged"),
            "ok": sum(1 for g in self.loops if g.state == "ok"),
            "deficit": len(self.deficits),
            "ungauged": sum(1 for g in self.loops if g.state == "ungauged"),
            "paging": sum(1 for g in self.loops if g.pages),
            "by_severity": by_sev,
            "by_class": by_class,
        }


def severity_for_ratio(ratio: float) -> str:
    """The shared severity ramp: 1x medium, 2x high, 4x critical.

    Below 1.0 the loop has not cleared its own bar and is not a deficit at
    all; the caller decides that before asking for a severity.
    """
    if ratio >= 4.0:
        return "critical"
    if ratio >= 2.0:
        return "high"
    if ratio >= 1.0:
        return "medium"
    return "low"


def _aware(dt: Any) -> Optional[datetime]:
    """Normalize a possibly-naive timestamp to aware UTC; None passes through."""
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _minutes_since(then: Optional[datetime], now: datetime) -> Optional[float]:
    aware = _aware(then)
    if aware is None:
        return None
    return max(0.0, (now - aware).total_seconds() / _MINUTE)


def _ungauged(
    loop_class: str, loop_id: str, label: str, reason: str, **evidence: Any
) -> LoopGauge:
    return LoopGauge(
        loop_class=loop_class,
        loop_id=loop_id,
        label=label,
        state="ungauged",
        quiet_reason=reason,
        evidence=dict(evidence),
    )


# ---------------------------------------------------------------------------
# Pure judgment — analyst cadence
# ---------------------------------------------------------------------------


def judge_analyst_cadence(
    row: Mapping[str, Any], *, now: datetime, cfg: GaugeConfig
) -> LoopGauge:
    """Did this analyst FIRE as often as its own descriptor promised?

    ``row`` carries ``analyst_id``, ``state``, ``cron``, ``last_run_at``,
    ``runs``, ``failed_runs`` and ``head_created_at``.
    """
    analyst_id = str(row.get("analyst_id") or "")
    label = f"analyst {analyst_id}"
    state = row.get("state")
    if state != "active":
        return _ungauged(
            LOOP_ANALYST_CADENCE, analyst_id, label, QUIET_NOT_ACTIVE,
            declared_state=state,
        )

    cron = (row.get("cron") or "").strip()
    if not cron:
        # The on-demand kinds (consult_default / deep_consult) and analysts
        # under a declared cadence freeze. No promise, no expectation.
        return _ungauged(
            LOOP_ANALYST_CADENCE, analyst_id, label, QUIET_NO_CADENCE,
        )
    interval = cadence_interval_minutes(cron)
    if interval is None or interval <= 0:
        return _ungauged(
            LOOP_ANALYST_CADENCE, analyst_id, label, QUIET_UNPARSABLE_CADENCE,
            cron=cron,
        )

    bar = max(cfg.analyst_missed_periods * interval, cfg.analyst_min_absence_minutes)
    last_run_at = _aware(row.get("last_run_at"))
    age = _minutes_since(last_run_at, now)

    if age is None:
        # Never ran. That is a real condition, but a descriptor registered
        # minutes ago has not had a chance yet — grace it against its own
        # interval, then treat it exactly like any other silence.
        since_head = _minutes_since(row.get("head_created_at"), now)
        if since_head is None or since_head < bar:
            return _ungauged(
                LOOP_ANALYST_CADENCE, analyst_id, label, QUIET_ACTIVATION_GRACE,
                cron=cron,
                interval_minutes=round(interval, 1),
                registered_minutes_ago=(
                    None if since_head is None else round(since_head, 1)
                ),
            )
        age = since_head

    ratio = age / bar
    evidence = {
        "cron": cron,
        "interval_minutes": round(interval, 1),
        "bar_minutes": round(bar, 1),
        "silent_minutes": round(age, 1),
        "missed_periods": round(age / interval, 2),
        "runs_in_window": int(row.get("runs") or 0),
        "failed_runs_in_window": int(row.get("failed_runs") or 0),
        "never_ran": last_run_at is None,
    }
    expected = (
        f"a run every {interval / 60.0:.1f}h ({cron}); silence past "
        f"{bar / 60.0:.1f}h is a deficit"
    )
    if last_run_at is None:
        actual = f"NEVER ran — registered {age / 60.0:.1f}h ago"
    else:
        actual = f"last run {age / 60.0:.1f}h ago ({age / interval:.1f} intervals)"

    return LoopGauge(
        loop_class=LOOP_ANALYST_CADENCE,
        loop_id=analyst_id,
        label=label,
        state="deficit" if ratio >= 1.0 else "ok",
        severity=severity_for_ratio(ratio),
        ratio=round(ratio, 3),
        expected=expected,
        actual=actual,
        last_production_at=last_run_at,
        evidence=evidence,
    )


# ---------------------------------------------------------------------------
# Pure judgment — analyst production
# ---------------------------------------------------------------------------


def judge_analyst_production(
    row: Mapping[str, Any], *, now: datetime, cfg: GaugeConfig
) -> LoopGauge:
    """Did this analyst, while RUNNING, still WRITE anything?

    The green-liveness failure class. ``row`` adds ``producing_runs``,
    ``last_producing_run_at`` and ``runs_since_production`` to the cadence
    row's fields.
    """
    analyst_id = str(row.get("analyst_id") or "")
    label = f"analyst {analyst_id}"
    if row.get("state") != "active":
        return _ungauged(
            LOOP_ANALYST_PRODUCTION, analyst_id, label, QUIET_NOT_ACTIVE,
            declared_state=row.get("state"),
        )
    if row.get("gather_only"):
        # A gather_only analyst legitimately NOOPs whenever there is nothing
        # to gather — its own descriptor says so.
        return _ungauged(
            LOOP_ANALYST_PRODUCTION, analyst_id, label, QUIET_GATHER_ONLY,
        )

    runs = int(row.get("runs") or 0)
    producing = int(row.get("producing_runs") or 0)
    if runs == 0:
        return _ungauged(
            LOOP_ANALYST_PRODUCTION, analyst_id, label, QUIET_NEVER_RAN,
        )
    if producing == 0:
        # Never wrote a row across the whole window: a side-effect sweep, by
        # observation rather than by a maintained list. Honest limit, stated
        # in the evidence: a producer dead LONGER than the window reads this
        # way too — which is why the window is long and why the cadence class
        # covers the case where it stopped running as well.
        return _ungauged(
            LOOP_ANALYST_PRODUCTION, analyst_id, label, QUIET_TRACE_ONLY,
            runs_in_window=runs,
            window_days=cfg.window_days,
        )
    if producing < cfg.analyst_min_producing_runs:
        return _ungauged(
            LOOP_ANALYST_PRODUCTION, analyst_id, label, QUIET_INSUFFICIENT_HISTORY,
            producing_runs=producing,
            required=cfg.analyst_min_producing_runs,
        )

    runs_per_output = runs / producing
    bar = max(
        cfg.analyst_drought_multiple * runs_per_output,
        float(cfg.analyst_min_runs_since),
    )
    since = float(row.get("runs_since_production") or 0)
    ratio = since / bar
    last_prod = _aware(row.get("last_producing_run_at"))

    return LoopGauge(
        loop_class=LOOP_ANALYST_PRODUCTION,
        loop_id=analyst_id,
        label=label,
        state="deficit" if ratio >= 1.0 else "ok",
        severity=severity_for_ratio(ratio),
        ratio=round(ratio, 3),
        expected=(
            f"a row about every {runs_per_output:.1f} runs (its own "
            f"{cfg.window_days}d rate); {bar:.0f} barren runs is a deficit"
        ),
        actual=f"{since:.0f} runs since its last row",
        last_production_at=last_prod,
        evidence={
            "runs_in_window": runs,
            "producing_runs": producing,
            "runs_per_output": round(runs_per_output, 2),
            "bar_runs": round(bar, 1),
            "runs_since_production": int(since),
            "failed_runs_in_window": int(row.get("failed_runs") or 0),
        },
    )


# ---------------------------------------------------------------------------
# Pure judgment — source production
# ---------------------------------------------------------------------------


def judge_source_production(
    row: Mapping[str, Any], *, now: datetime, cfg: GaugeConfig
) -> LoopGauge:
    """Did this source, while POLLING healthily, still CONTRIBUTE anything?

    ``row`` carries ``source_id``, ``state``, ``cron``, ``head_created_at``,
    ``signals`` (count in window), ``last_created_at``
    (``max(signals.created_at)`` — never ``fetched_at``), ``max_gap_minutes``
    (largest observed inter-arrival gap in the window), ``polls_ok`` /
    ``polls_error`` (window) and ``polls_since_production``.
    """
    source_id = str(row.get("source_id") or "")
    label = f"source {source_id}"
    if row.get("state") != "active":
        return _ungauged(
            LOOP_SOURCE_PRODUCTION, source_id, label, QUIET_NOT_ACTIVE,
            declared_state=row.get("state"),
        )

    polls_ok = int(row.get("polls_ok") or 0)
    polls_error = int(row.get("polls_error") or 0)
    polls_total = polls_ok + polls_error

    cron = (row.get("cron") or "").strip()
    interval = cadence_interval_minutes(cron) if cron else None
    quiet_floor = max(
        cfg.source_cadence_multiple * interval if interval else 0.0,
        cfg.source_min_drought_minutes,
    )

    # -- the stopped-poller branch ------------------------------------------
    # An ACTIVE descriptor with zero poll outcomes in the whole window used to
    # fall through to "insufficient history" and vanish (ungauged). That is
    # the one shape that must page the loudest: the descriptor says run, and
    # nothing is running (gdelt.doc_api went dark for 6 days this way,
    # 2026-08-09 — its actor had been retired out from under a live-looking
    # descriptor).
    if polls_total == 0:
        since_head = _minutes_since(row.get("head_created_at"), now)
        if since_head is not None and since_head < quiet_floor:
            return _ungauged(
                LOOP_SOURCE_PRODUCTION, source_id, label, QUIET_ACTIVATION_GRACE,
                registered_minutes_ago=round(since_head, 1),
                grace_minutes=round(quiet_floor, 1),
            )
        ratio = (since_head / quiet_floor) if since_head else 1.0
        return LoopGauge(
            loop_class=LOOP_SOURCE_PRODUCTION,
            loop_id=source_id,
            label=label,
            state="deficit",
            severity=severity_for_ratio(ratio),
            ratio=round(ratio, 3),
            expected=(
                f"an active source should be POLLED at all in "
                f"{cfg.window_days}d (its descriptor says run)"
            ),
            actual="NO POLLS — the poller/actor is not running for this source",
            last_production_at=_aware(row.get("lifetime_last_created_at")),
            evidence={
                "sub_state": "no_polls",
                "cron": cron or None,
                "window_days": cfg.window_days,
                "lifetime_signals": int(row.get("lifetime_signals") or 0),
            },
        )

    error_share = (polls_error / polls_total) if polls_total else 0.0
    if polls_total and error_share > cfg.source_max_error_share:
        # An erroring feed is already the watchdog's condition and the source
        # health plane's; calling it a production deficit too would double-page
        # the same fault under two names.
        return _ungauged(
            LOOP_SOURCE_PRODUCTION, source_id, label, QUIET_POLLING_ERRORS,
            polls_ok=polls_ok,
            polls_error=polls_error,
            error_share=round(error_share, 3),
        )

    signals = int(row.get("signals") or 0)
    last_created = _aware(row.get("last_created_at"))
    drought = _minutes_since(last_created, now)
    polls_since = int(row.get("polls_since_production") or 0)
    feed_newest = _aware(row.get("feed_newest_entry_ts"))
    lifetime = int(row.get("lifetime_signals") or 0)
    life_last = _aware(row.get("lifetime_last_created_at"))

    # -- the zero-in-window branch ------------------------------------------
    if signals == 0 or last_created is None:
        since_head = _minutes_since(row.get("head_created_at"), now)
        if since_head is not None and since_head < quiet_floor:
            # Registered too recently to have owed us anything yet. A feed's
            # first item can legitimately be a day out.
            return _ungauged(
                LOOP_SOURCE_PRODUCTION, source_id, label, QUIET_ACTIVATION_GRACE,
                polls_ok=polls_ok,
                registered_minutes_ago=round(since_head, 1),
                grace_minutes=round(quiet_floor, 1),
            )
        if polls_ok < cfg.source_min_polls_for_silent:
            return _ungauged(
                LOOP_SOURCE_PRODUCTION, source_id, label, QUIET_INSUFFICIENT_HISTORY,
                polls_ok=polls_ok,
                required=cfg.source_min_polls_for_silent,
            )
        # A source with LIFETIME production is not never-produced — it is a
        # sparse publisher whose whole output predates the window (eia.press
        # publishes ~monthly; a 21d window shows zero forever). The honest
        # discriminator is persisted on the poll rows: `newest_entry_ts` is
        # the newest entry the FEED itself carried. If the feed holds nothing
        # newer than what we already ingested, the quiet is upstream's, not
        # ours; if it does hold newer content and we produced nothing, THAT
        # is the real conversion deficit.
        if lifetime > 0 and life_last is not None and feed_newest is not None:
            if feed_newest <= life_last + timedelta(hours=6):
                return LoopGauge(
                    loop_class=LOOP_SOURCE_PRODUCTION,
                    loop_id=source_id,
                    label=label,
                    state="ok",
                    severity="low",
                    ratio=0.0,
                    expected="conversion of any feed entry newer than our last ingest",
                    actual=(
                        f"upstream quiet — the feed's newest entry "
                        f"({feed_newest.isoformat(timespec='seconds')}) is already "
                        f"ingested; {lifetime} lifetime signals, last "
                        f"{life_last.isoformat(timespec='seconds')}"
                    ),
                    last_production_at=life_last,
                    evidence={
                        "sub_state": "upstream_quiet",
                        "cron": cron or None,
                        "polls_ok": polls_ok,
                        "lifetime_signals": lifetime,
                        "feed_newest_entry_ts": feed_newest.isoformat(),
                        "window_days": cfg.window_days,
                    },
                )
            stale_minutes = _minutes_since(life_last, now) or 0.0
            ratio = stale_minutes / quiet_floor if quiet_floor else 1.0
            return LoopGauge(
                loop_class=LOOP_SOURCE_PRODUCTION,
                loop_id=source_id,
                label=label,
                state="deficit",
                severity=severity_for_ratio(ratio),
                ratio=round(ratio, 3),
                expected=(
                    "conversion: the feed carries entries newer than our last "
                    "ingest, so healthy polls should be producing"
                ),
                actual=(
                    f"feed newest entry "
                    f"{feed_newest.isoformat(timespec='seconds')} vs last ingest "
                    f"{life_last.isoformat(timespec='seconds')} — "
                    f"{polls_ok} healthy polls converted none of it"
                ),
                last_production_at=life_last,
                evidence={
                    "sub_state": "conversion_stall",
                    "cron": cron or None,
                    "polls_ok": polls_ok,
                    "lifetime_signals": lifetime,
                    "feed_newest_entry_ts": feed_newest.isoformat(),
                    "window_days": cfg.window_days,
                },
            )
        ratio = polls_ok / float(cfg.source_min_polls_for_silent)
        return LoopGauge(
            loop_class=LOOP_SOURCE_PRODUCTION,
            loop_id=source_id,
            label=label,
            state="deficit",
            severity=severity_for_ratio(ratio),
            ratio=round(ratio, 3),
            expected=(
                f"any signal at all: an active source polled "
                f"{cfg.source_min_polls_for_silent}+ times without error "
                f"should have produced something"
            ),
            actual=(
                f"SILENT — {polls_ok} healthy polls, 0 signals in "
                f"{cfg.window_days}d"
                + (f"; {lifetime} lifetime signals" if lifetime else " (never produced)")
            ),
            last_production_at=life_last,
            evidence={
                "sub_state": "silent",
                "cron": cron or None,
                "polls_ok": polls_ok,
                "polls_error": polls_error,
                "signals_in_window": 0,
                "lifetime_signals": lifetime,
                "window_days": cfg.window_days,
            },
        )

    # -- the drought branch ------------------------------------------------
    if signals < cfg.source_min_signals_for_gap:
        return _ungauged(
            LOOP_SOURCE_PRODUCTION, source_id, label, QUIET_INSUFFICIENT_HISTORY,
            signals_in_window=signals,
            required=cfg.source_min_signals_for_gap,
        )

    max_gap = float(row.get("max_gap_minutes") or 0.0)
    bar = max(cfg.source_gap_multiple * max_gap, quiet_floor)
    ratio = (drought or 0.0) / bar

    # Same upstream-quiet discriminator as the zero-in-window branch: a
    # would-be drought deficit is honest quiet when the feed itself holds
    # nothing newer than our last ingest (the weekday-publisher weekend,
    # 2026-08-09 — a dozen sources "froze" because their newsrooms went home).
    if (
        ratio >= 1.0
        and feed_newest is not None
        and last_created is not None
        and feed_newest <= last_created + timedelta(hours=6)
    ):
        return LoopGauge(
            loop_class=LOOP_SOURCE_PRODUCTION,
            loop_id=source_id,
            label=label,
            state="ok",
            severity="low",
            ratio=0.0,
            expected="conversion of any feed entry newer than our last ingest",
            actual=(
                f"upstream quiet — last signal "
                f"{(drought or 0.0) / 60.0:.1f}h ago but the feed's newest entry "
                f"({feed_newest.isoformat(timespec='seconds')}) is already ingested"
            ),
            last_production_at=last_created,
            evidence={
                "sub_state": "upstream_quiet",
                "cron": cron or None,
                "drought_minutes": round(drought or 0.0, 1),
                "feed_newest_entry_ts": feed_newest.isoformat(),
                "signals_in_window": signals,
                "polls_ok": polls_ok,
            },
        )

    return LoopGauge(
        loop_class=LOOP_SOURCE_PRODUCTION,
        loop_id=source_id,
        label=label,
        state="deficit" if ratio >= 1.0 else "ok",
        severity=severity_for_ratio(ratio),
        ratio=round(ratio, 3),
        expected=(
            f"a signal at least every {bar / 60.0:.1f}h "
            f"({cfg.source_gap_multiple:g}x its own {max_gap / 60.0:.1f}h "
            f"worst observed gap, floored by cadence)"
        ),
        actual=f"last signal {(drought or 0.0) / 60.0:.1f}h ago",
        last_production_at=last_created,
        evidence={
            "sub_state": "drought",
            "cron": cron or None,
            "poll_interval_minutes": None if interval is None else round(interval, 1),
            "max_observed_gap_minutes": round(max_gap, 1),
            "bar_minutes": round(bar, 1),
            "drought_minutes": round(drought or 0.0, 1),
            "signals_in_window": signals,
            "polls_ok": polls_ok,
            "polls_error": polls_error,
            "polls_since_production": polls_since,
            "window_days": cfg.window_days,
        },
    )


# ---------------------------------------------------------------------------
# Pure judgment — backlog drain
# ---------------------------------------------------------------------------


def judge_backlog_drain(
    drain: BacklogDrain,
    row: Mapping[str, Any],
    *,
    now: datetime,
    cfg: GaugeConfig,
) -> LoopGauge:
    """Is declared-overdue work being drained by the analyst that owns it?

    ``row`` carries ``overdue``, ``oldest_due_at``, ``resolved`` (in window)
    and ``owner_runs`` (in window).
    """
    label = drain.label
    overdue = int(row.get("overdue") or 0)
    resolved = int(row.get("resolved") or 0)
    owner_runs = int(row.get("owner_runs") or 0)
    oldest = _aware(row.get("oldest_due_at"))
    overdue_age_days = (
        None if oldest is None else round((now - oldest).total_seconds() / 86_400.0, 1)
    )

    if overdue == 0:
        return _ungauged(
            LOOP_BACKLOG_DRAIN, drain.backlog_id, label, QUIET_NO_BACKLOG,
            resolved_in_window=resolved,
            owner_runs=owner_runs,
        )
    if owner_runs < cfg.backlog_min_owner_runs:
        # The resolver is not running: that is a CADENCE deficit, already
        # gauged under its own analyst id. Attributing it here too would
        # report one fault twice.
        return _ungauged(
            LOOP_BACKLOG_DRAIN, drain.backlog_id, label, QUIET_OWNER_NOT_RUNNING,
            overdue=overdue,
            owner_runs=owner_runs,
            required=cfg.backlog_min_owner_runs,
            owner_analyst_id=drain.owner_analyst_id,
        )

    if resolved > 0:
        ratio = 0.0
        state: GaugeState = "ok"
    else:
        # Every owner run is one whole missed opportunity to drain. The ramp
        # is the run count against the same "enough runs to be fair" floor,
        # so a resolver that has been failing for weeks escalates on its own.
        ratio = owner_runs / float(cfg.backlog_min_owner_runs)
        state = "deficit"

    return LoopGauge(
        loop_class=LOOP_BACKLOG_DRAIN,
        loop_id=drain.backlog_id,
        label=label,
        state=state,
        severity=severity_for_ratio(ratio) if state == "deficit" else "info",
        ratio=round(ratio, 3),
        expected=(
            f"{overdue} overdue {drain.unit}(s) and "
            f"{drain.owner_analyst_id} running — at least one resolution in "
            f"{cfg.window_days}d"
        ),
        actual=(
            f"{resolved} resolved across {owner_runs} owner run(s)"
            + (
                f"; oldest overdue {overdue_age_days}d"
                if overdue_age_days is not None
                else ""
            )
        ),
        last_production_at=None,
        evidence={
            "overdue": overdue,
            "resolved_in_window": resolved,
            "owner_analyst_id": drain.owner_analyst_id,
            "owner_runs_in_window": owner_runs,
            "oldest_overdue_at": oldest.isoformat() if oldest else None,
            "oldest_overdue_age_days": overdue_age_days,
            "window_days": cfg.window_days,
        },
    )


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

#: Analyst expectation + actual in one pass. The ``produced`` predicate is the
#: UNION that makes this kind-agnostic: ``output_row_refs`` catches every
#: OutputKind routed to its own table (journal rows land in
#: ``journal_entries``), and the ``analyst_outputs.run_id`` existence catches
#: TRACE_ONLY handlers that side-write their real product (scorecards, alerts).
#: Neither alone is sufficient; together they need no per-kind table map.
_ANALYST_SQL = """
    WITH heads AS (
        SELECT d.descriptor_id AS analyst_id,
               d.state,
               d.created_at    AS head_created_at,
               NULLIF(btrim(coalesce(
                   d.body->'cadence'->>'fallback_schedule', '')), '') AS cron,
               coalesce(
                   (d.body->'subscription'->'substrate'->>'gather_only')::boolean,
                   false) AS gather_only
          FROM analyst_descriptors d
         WHERE d.is_head
         LIMIT $2
    ),
    -- Materialized ONCE and hash-joined rather than probed per trace: the
    -- trace table carries ~80k rows in a 21-day window on the live fleet and
    -- a correlated EXISTS turned this leg into a 1.9s query. The set is small
    -- (one row per run that wrote anything, off the partial run_id index).
    prod_runs AS (
        SELECT DISTINCT run_id
          FROM analyst_outputs
         WHERE run_id IS NOT NULL
    ),
    scan AS (
        SELECT t.analyst_id,
               t.run_started_at,
               t.status,
               (cardinality(t.output_row_refs) > 0
                OR pr.run_id IS NOT NULL) AS produced
          FROM analyst_traces t
          LEFT JOIN prod_runs pr ON pr.run_id = t.run_id
         WHERE t.run_started_at > now() - make_interval(days => $1)
    ),
    -- One pass for the runs-since count too: the correlated COUNT it replaces
    -- re-scanned the whole window for every trace-only sweep (cross_source_dedup
    -- alone is ~78k rows) to produce a number that sweep never uses.
    windowed AS (
        SELECT s.*,
               max(run_started_at) FILTER (WHERE produced)
                   OVER (PARTITION BY analyst_id) AS last_prod
          FROM scan s
    ),
    agg AS (
        SELECT analyst_id,
               count(*)::int                                    AS runs,
               count(*) FILTER (WHERE status <> 'success')::int  AS failed_runs,
               count(*) FILTER (WHERE produced)::int             AS producing_runs,
               max(run_started_at) FILTER (WHERE produced)
                   AS last_producing_run_at,
               -- A window with no producing run at all counts EVERY run as
               -- barren, matching "runs since the last row" when there is no
               -- last row.
               count(*) FILTER (
                   WHERE last_prod IS NULL OR run_started_at > last_prod
               )::int                                           AS runs_since_production
          FROM windowed
         GROUP BY 1
    )
    SELECT h.analyst_id,
           h.state,
           h.cron,
           h.gather_only,
           h.head_created_at,
           coalesce(a.runs, 0)           AS runs,
           coalesce(a.failed_runs, 0)    AS failed_runs,
           coalesce(a.producing_runs, 0) AS producing_runs,
           coalesce(a.runs_since_production, 0) AS runs_since_production,
           a.last_producing_run_at,
           -- Newest run of ALL time: a cadence deficit must not be capped at
           -- the window, or an analyst dead for longer than the window would
           -- read as "never ran" rather than "dead for N periods". One index-
           -- only backward scan on (analyst_id, run_started_at DESC).
           (SELECT max(t2.run_started_at) FROM analyst_traces t2
             WHERE t2.analyst_id = h.analyst_id)              AS last_run_at
      FROM heads h
      LEFT JOIN agg a ON a.analyst_id = h.analyst_id
"""

#: Source expectation + actual. Production is ``signals.created_at`` — the row
#: birth — never ``fetched_at``, which a no-op poll bumps (see the module
#: docstring: that substitution is precisely why the frozen AP feeds were
#: invisible for six days).
_SOURCE_SQL = """
    WITH heads AS (
        SELECT d.descriptor_id AS source_id,
               d.state,
               d.created_at    AS head_created_at,
               NULLIF(btrim(coalesce(
                   jsonb_path_query_first(d.body, '$.**.schedule.raw') #>> '{}',
                   '')), '') AS cron
          FROM source_descriptors d
         WHERE d.is_head
           AND d.descriptor_id NOT LIKE ALL ($3::text[])
         LIMIT $2
    ),
    gaps AS (
        SELECT source_id,
               created_at,
               EXTRACT(epoch FROM created_at - lag(created_at)
                   OVER (PARTITION BY source_id ORDER BY created_at)) / 60.0
                   AS gap_minutes
          FROM signals
         WHERE created_at > now() - make_interval(days => $1)
    ),
    sig AS (
        SELECT source_id,
               count(*)::int      AS signals,
               max(created_at)    AS last_created_at,
               coalesce(max(gap_minutes), 0.0)::float8 AS max_gap_minutes
          FROM gaps
         GROUP BY 1
    ),
    pol AS (
        SELECT source_id,
               count(*) FILTER (WHERE outcome <> 'error')::int AS polls_ok,
               count(*) FILTER (WHERE outcome =  'error')::int AS polls_error,
               max(newest_entry_ts)                            AS feed_newest_entry_ts
          FROM source_poll_outcomes
         WHERE occurred_at > now() - make_interval(days => $1)
         GROUP BY 1
    ),
    life AS (
        -- Lifetime production facts, NOT windowed: a monthly publisher has
        -- zero signals in any 21d window and must not read as never-produced.
        SELECT source_id,
               count(*)::int   AS lifetime_signals,
               max(created_at) AS lifetime_last_created_at
          FROM signals
         GROUP BY 1
    )
    SELECT h.source_id,
           h.state,
           h.cron,
           h.head_created_at,
           coalesce(s.signals, 0)           AS signals,
           s.last_created_at,
           coalesce(s.max_gap_minutes, 0.0) AS max_gap_minutes,
           coalesce(p.polls_ok, 0)          AS polls_ok,
           coalesce(p.polls_error, 0)       AS polls_error,
           p.feed_newest_entry_ts,
           coalesce(l.lifetime_signals, 0)  AS lifetime_signals,
           l.lifetime_last_created_at,
           (SELECT count(*)::int FROM source_poll_outcomes po
             WHERE po.source_id = h.source_id
               AND po.outcome <> 'error'
               AND po.occurred_at > coalesce(
                     s.last_created_at,
                     now() - make_interval(days => $1)))  AS polls_since_production
      FROM heads h
      LEFT JOIN sig  s ON s.source_id = h.source_id
      LEFT JOIN pol  p ON p.source_id = h.source_id
      LEFT JOIN life l ON l.source_id = h.source_id
"""

_OWNER_RUNS_SQL = """
    SELECT count(*)::int AS owner_runs
      FROM analyst_traces
     WHERE analyst_id = $1
       AND run_started_at > $2
"""


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


async def read_analyst_loops(
    conn: Any, *, now: datetime, cfg: GaugeConfig
) -> list[LoopGauge]:
    """Both analyst classes from one query (cadence + production)."""
    rows = await conn.fetch(_ANALYST_SQL, cfg.window_days, _MAX_ANALYSTS)
    out: list[LoopGauge] = []
    for row in rows:
        mapping = dict(row)
        out.append(judge_analyst_cadence(mapping, now=now, cfg=cfg))
        out.append(judge_analyst_production(mapping, now=now, cfg=cfg))
    return out


async def read_source_loops(
    conn: Any, *, now: datetime, cfg: GaugeConfig
) -> list[LoopGauge]:
    junk = [f"{p}%" for p in JUNK_SOURCE_PREFIXES]
    rows = await conn.fetch(_SOURCE_SQL, cfg.window_days, _MAX_SOURCES, junk)
    return [judge_source_production(dict(r), now=now, cfg=cfg) for r in rows]


async def read_backlog_loops(
    conn: Any,
    *,
    now: datetime,
    cfg: GaugeConfig,
    drains: Sequence[BacklogDrain] = BACKLOG_DRAINS,
) -> list[LoopGauge]:
    """The declared drains.

    A drain whose SQL will not run (an unapplied migration on a fresh
    substrate, a renamed column) degrades to ``ungauged`` with the error
    recorded — LOUD in the evidence, never a silent zero that would read as
    "the backlog is clear".
    """
    window_start = now - timedelta(days=cfg.window_days)
    out: list[LoopGauge] = []
    for drain in drains:
        try:
            overdue_row = await conn.fetchrow(drain.overdue_sql)
            resolved_row = await conn.fetchrow(drain.resolved_sql, window_start)
            owner_row = await conn.fetchrow(
                _OWNER_RUNS_SQL, drain.owner_analyst_id, window_start
            )
        except Exception as exc:  # noqa: BLE001 — a broken drain must SAY so
            logger.warning(
                "production_gauge.backlog_unavailable backlog=%s err=%s",
                drain.backlog_id,
                exc,
            )
            out.append(
                _ungauged(
                    LOOP_BACKLOG_DRAIN,
                    drain.backlog_id,
                    drain.label,
                    "backlog_query_failed",
                    error=f"{type(exc).__name__}: {exc}"[:400],
                    owner_analyst_id=drain.owner_analyst_id,
                )
            )
            continue
        merged = {
            "overdue": (overdue_row or {}).get("overdue", 0),
            "oldest_due_at": (overdue_row or {}).get("oldest_due_at"),
            "resolved": (resolved_row or {}).get("resolved", 0),
            "owner_runs": (owner_row or {}).get("owner_runs", 0),
        }
        out.append(judge_backlog_drain(drain, merged, now=now, cfg=cfg))
    return out


async def read_gauge(
    conn: Any,
    *,
    now: Optional[datetime] = None,
    cfg: Optional[GaugeConfig] = None,
) -> GaugeReport:
    """The whole-engine read: every producing loop, worst-first.

    Sorting is deficit-before-ok-before-ungauged, then by severity, then by
    ratio — so the top of the table is always the thing most worth reading,
    which is the entire point of a one-screen operator surface.
    """
    now = now or datetime.now(tz=timezone.utc)
    cfg = cfg or GaugeConfig()
    loops: list[LoopGauge] = []
    loops.extend(await read_analyst_loops(conn, now=now, cfg=cfg))
    loops.extend(await read_source_loops(conn, now=now, cfg=cfg))
    loops.extend(await read_backlog_loops(conn, now=now, cfg=cfg))
    # The INTEGRITY loops (judge availability, descriptor prompt drift). Imported
    # at call time: they import LoopGauge/_ungauged/severity_for_ratio from here,
    # so a module-level import either way round would be a cycle.
    from .production_gauge_integrity import read_integrity_loops

    loops.extend(await read_integrity_loops(conn, now=now, cfg=cfg))
    _STATE_ORDER = {"deficit": 0, "ok": 1, "ungauged": 2}
    loops.sort(
        key=lambda g: (
            _STATE_ORDER.get(g.state, 3),
            -g.rank,
            -(g.ratio or 0.0),
            g.loop_class,
            g.loop_id,
        )
    )
    return GaugeReport(generated_at=now, window_days=cfg.window_days, loops=loops)


__all__ = [
    "ALERT_MIN_SEVERITY",
    "BACKLOG_DRAINS",
    "BacklogDrain",
    "GaugeConfig",
    "GaugeReport",
    "JUNK_SOURCE_PREFIXES",
    "LOOP_ANALYST_CADENCE",
    "LOOP_ANALYST_PRODUCTION",
    "LOOP_BACKLOG_DRAIN",
    "LOOP_DESCRIPTOR_PROMPT_DRIFT",
    "LOOP_DESCRIPTOR_STATE_DRIFT",
    "LOOP_JUDGE_AVAILABILITY",
    "LOOP_CLASSES",
    "LOOP_SOURCE_PRODUCTION",
    "LoopGauge",
    "SEVERITY_RANK",
    "judge_analyst_cadence",
    "judge_analyst_production",
    "judge_backlog_drain",
    "judge_source_production",
    "read_analyst_loops",
    "read_backlog_loops",
    "read_gauge",
    "read_source_loops",
    "severity_for_ratio",
]
