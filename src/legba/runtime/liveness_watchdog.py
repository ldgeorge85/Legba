# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Silent-stall liveness watchdog (resilience-observability).

A live Legba rig is a pipeline: sources publish signals
(``legba.signals.>``), the fan-out → trigger path fires analyst runs, and
analysts publish outputs (``analyst.<id>.finding`` and the other output kinds
under ``analyst.>``). When that pipeline silently stalls — a wedged source
poller, a stuck consumer, a crashed worker pool — nothing errors; the system
simply goes quiet. Without a watchdog the only signal an operator gets is the
*absence* of data, which is exactly what humans are worst at noticing.

This watchdog turns silence into a loud signal. It passively observes the two
liveness subjects with lightweight core-NATS subscriptions (no durable
consumer, no acks — it must not perturb the JetStream delivery the real planes
depend on), records the wall-clock time of the last message on each, and on a
fixed cadence checks the gap. If *neither* a signal nor a finding has arrived
within ``stall_after`` it emits a ``high``-severity stall alert on
``legba.alerts.high`` — the same subject + envelope shape the ``alert`` output
kind uses, so the UI live-feed and any operator sink pick it up with no extra
wiring. Re-alerts are rate-limited by ``realert_every`` so a prolonged outage
produces a heartbeat, not a flood; recovery (any message after a stall) clears
the latch and is itself logged.

The watchdog is intentionally tolerant of a *cold* rig: it does not fire until
it has seen at least one message OR ``stall_after`` has elapsed since start, so
a freshly-booted, not-yet-seeded stack doesn't immediately self-alert. (Boot
grace = ``stall_after`` from construction time.)

B0-12 (durable per-entity alerts + precision): the per-analyst / per-source
cadence checks and the per-source empty-streak check alert on the
STATE-TRANSITION EDGE — one durable ``alert_sink_deliveries`` row
(``sink_kind='liveness_watchdog'``, ``payload_summary.state``
'entered'/'recovered') plus an outward fan-out per transition, silence
while a condition persists, restart-safe via a ledger-seeded state map. The
empty-streak check additionally discriminates honest-quiet feeds (newest
observed upstream entry <= our last ingest → silent below a much higher
prolonged-quiet bound, escalated past it) from cursor/filter faults (upstream
carries newer entries yet polls yield 0 → escalate) using the
``newest_entry_ts`` evidence the source handlers record per poll.

L1 (2026-09-20) adds a fourth durable channel, ``llm_route_dead`` — a busy LLM
component answering almost nothing (a model retired upstream) is a liveness
fault every check above reads as health. See :mod:`.llm_route_liveness`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import alert_volume_watch as _volume_watch
from . import analyst_cursor_lag as _cursor_lag
from . import consult_health as _consult_health
from . import llm_route_liveness as _llm_route
from . import search_degraded_watch as _search_watch
from . import source_staleness_watch as _stale_watch
from . import stream_liveness as _stream_watch
from . import volume_surge_watch as _surge_watch
from .cadence_staleness import evaluate_cadence_staleness as _evaluate_cadence_staleness
from .source_first_poll_age import DEFAULT_FIRST_POLL_MAX_AGE  # noqa: F401 — re-exported
from .source_quiet_evidence import (  # noqa: F401 — re-exported
    _as_aware_utc,
    _DEFAULT_HONEST_QUIET_STREAK,
    _classify_streak,
    _evaluate_empty_streaks,
    _source_stall_is_honest_quiet,
)
from .source_stall_diagnosis import _source_stall_diagnosis
from . import source_stall_diagnosis as _ssd

logger = logging.getLogger(__name__)

# Subjects the watchdog treats as "the pipeline is alive".
SIGNAL_WILDCARD = "legba.signals.>"
# Analyst outputs (finding / situation / hypothesis / prediction / meta_finding
# / critique / fact / nexus) all publish under ``analyst.<id>.<kind>``.
FINDING_WILDCARD = "analyst.>"
# Stall alerts go out on the standard alert subject so existing subscribers
# (UI live feed, operator sinks) receive them unchanged.
STALL_ALERT_SUBJECT = "legba.alerts.high"

# Env knobs (minutes for the operator-facing ones; seconds internally).
_STALL_AFTER_ENV = "LEGBA_WATCHDOG_STALL_MINUTES"
_REALERT_ENV = "LEGBA_WATCHDOG_REALERT_MINUTES"
_INTERVAL_ENV = "LEGBA_WATCHDOG_CHECK_SECONDS"

_DEFAULT_STALL_MINUTES = 15.0
_DEFAULT_REALERT_MINUTES = 30.0
_DEFAULT_CHECK_SECONDS = 60.0

# OBS — per-analyst cadence-liveness (extension). The GLOBAL stall
# check above can't see ONE analyst going dark while the aggregate pipeline
# keeps flowing: the world_assessor (and both grounded assessors) sat dead
# ~24h behind a registry-deserialize break while cross_source_dedup et al. kept
# publishing, so neither the signal nor the finding wildcard ever went quiet.
# OBS closes that hole: for each ACTIVE cadence-bearing analyst, alert when its
# newest successful run is older than ``factor`` × its own cron interval.
_CADENCE_FACTOR_ENV = "LEGBA_CADENCE_STALL_FACTOR"
_DEFAULT_CADENCE_FACTOR = 2.0
# Floor on the per-analyst threshold so a fast-cadence analyst (e.g. */5)
# can't trip on a single missed tick (jitter, a cooldown landing just past a
# fire, a brief reconcile gap). 90 min is well above the 60s check interval and
# the longest deterministic cooldown, but far below the 6h meta cadences.
_CADENCE_MIN_THRESHOLD_S = 90.0 * 60.0

# R2-FIX (2026-09-17) — A BUDGET PAUSE IS NOT A STALL, and the fleet had no way
# to tell them apart. When an analyst's per-day token bucket is exhausted, the
# actor's own precall check returns ``exhausted``, writes a
# ``budget_demotion_events`` row and — under the default
# ``pause_until_next_window`` strategy — stamps ``cooldown_until`` for
# ``BudgetRetryPolicy.cooldown_seconds`` (1 h). Every tick inside that window
# then returns ``noop reason=cooldown scope=global`` and writes NO
# ``analyst_traces`` row at all, because it returns before the run body. So the
# query below sees the analyst's newest SUCCESSFUL run getting older and older
# and calls it a stall — which is exactly what happened to ``reference_builder``
# at 02:43Z: the budget doing precisely its job, reported as the analyst going
# dark. The operator is sent to "check actor activation, descriptor head
# version, deps resolution" for a condition none of those explain.
#
# The suppression is BOUNDED and it is LOUD. Bounded: it lasts one cooldown
# window from the demotion, so an analyst that is chronically exhausted still
# alerts once the window lapses — being out of budget every day IS worth
# knowing, and this must not become the way that fact hides. Loud: every
# suppression logs the analyst, the age and the demotion that explains it, so
# "no alert" is never the same as "nothing happened".
#
# WHAT IT DOES NOT COVER, stated rather than discovered later: the ``throttle``
# decision (a projected overrun) also stamps a cooldown but writes NO audit row,
# so it is invisible from Postgres. Closing that would mean writing a row from
# the actor's throttle branch — in ``dapr_actors.py``, which is frozen. It is
# also the rarer branch: ``throttle`` needs a non-zero per-run estimate, which
# the deterministic wiring does not set.
_BUDGET_PAUSE_GRACE_ENV = "LEGBA_CADENCE_STALL_BUDGET_PAUSE_GRACE_S"
#: Matches ``BudgetRetryPolicy.cooldown_seconds``'s own default (1 h).
_DEFAULT_BUDGET_PAUSE_GRACE_S = 3600.0

# OBS — per-SOURCE cadence-liveness (DQ-H5). Sources poll on their own crons
# (hourly … 6-hourly); the sweep found 10 active sources silent >7d (feeds
# HTTP-200 but the runtime poll wrote 0 signals + 0 error rows — invisible).
# Same factor × interval rule, but a higher floor so a single missed poll on a
# 6-hourly source can't false-alarm.
_SOURCE_CADENCE_MIN_THRESHOLD_S = 3.0 * 3600.0

# OBS — per-SOURCE empty-streak escalation (D19). The cadence check above only
# trips once a source crosses its (factor × poll-cron) signal-staleness window,
# which for a 6-hourly source is many hours; meanwhile xinhua.world +
# aljazeera.arabic sat returning HTTP-200-but-0-items for 15 DAYS while every
# poll-outcome row logged health_state='healthy' — a slow-but-running feed that
# is functionally DEAD. This check is the faster, source-specific signal: count
# the source's most-recent CONSECUTIVE 'empty' poll outcomes (un-broken by any
# productive poll or any 'error' row) and, once the run reaches
# ``empty_streak_threshold``, emit a 'degraded'-flavoured alert (reusing the
# analyst_outputs kind='alert' path — NO new table). It complements rather than
# replaces the cadence check: a feed can be empty-streaked long before its
# cadence window expires.
_EMPTY_STREAK_ENV = "LEGBA_SOURCE_EMPTY_STREAK_THRESHOLD"
_DEFAULT_EMPTY_STREAK = 5
# Floor for how many recent poll-outcome rows per source the streak read pulls
# back. The streak only counts leading 'empty' rows, so a window comfortably
# above the threshold is enough to confirm the run and see the breaking row
# (if any). This is a FLOOR, not the effective window — see
# ``_empty_streak_fetch_window`` below: the real per-check window is sized
# dynamically off the CONFIGURED thresholds, because a fixed floor alone
# already broke once (2026-08-29 DQ sweep, finding 1).
_EMPTY_STREAK_WINDOW = 20
# 2026-08-29 DQ sweep (finding 1) — the FETCH WINDOW must strictly exceed
# every threshold the streak is compared against, or the threshold is
# structurally unreachable no matter how long the real streak runs.
# ``_fetch_source_empty_streak_rows`` LIMITs its per-source pull, and
# ``_evaluate_empty_streaks`` can only ever report a streak as long as the
# rows it was handed. That is exactly how ``honest_quiet_streak_threshold``
# (added below, default 36) shipped DEAD: the fixed window above (20) is
# LESS than 36, so ``streak < honest_quiet_threshold`` was always true —
# verified live: 8 sources pinned at exactly streak 20, zero
# ``honest_quiet_prolonged`` alerts ever, source.who.news 0 signals/32 days /
# 120 consecutive empty polls / active+healthy, unalerted. This margin is the
# same shape as the sibling fix in
# ``legba.data.analysts.deterministic_handlers.entity_gc._SOURCE_STREAK_WINDOW``
# (``max(threshold + 5, floor)`` — "must exceed ... so a qualifying leading
# run is never truncated by the LIMIT"), generalized to TWO independently
# env-tunable thresholds so a future operator raising either one can't
# silently reopen this gap. A bounded fetch sized off the configured
# thresholds (rather than an unbounded per-source scan) is the cheaper of the
# two structurally-correct fixes: a genuinely dead
# source polled on a tight cadence (who.news: every ~15 min) can accumulate
# an unbounded empty run over months, and nothing downstream needs to see
# past "more rows than the highest configured threshold, plus enough slack to
# also observe the breaking row" to prove the escalation correctly fires —
# it does not need the EXACT true streak length, only a floor-correct one
# (a streak longer than the window reports as ``window`` rows, which still
# clears the threshold and still alerts; see
# ``INGESTION_SMALLS_REPORT.md`` for the accepted precision trade-off).
_EMPTY_STREAK_WINDOW_SLACK = 5

# 2026-08-27 DQ sweep — the HONEST-QUIET ESCALATION BOUND. B0-12's
# ``_classify_streak`` correctly exempts a run whose own crawl evidence shows
# nothing newer than what is already ingested ('honest_quiet' — the weekly/
# monthly-feed false-positive class), but that exemption had NO ceiling: it
# silenced the streak at ANY length. The sweep's source.wto.news case is
# exactly that gap wearing a different number — 110 consecutive empty/healthy
# polls over 9 days, every one honestly evidenced as quiet by the same
# discriminator, so ``state='active'`` and ``health_state='healthy'`` never
# wavered while the source produced nothing at all. A single quiet week is
# ordinary; a streak this long is operationally indistinguishable from the
# source having died outright (moved, retired, or a parser silently
# returning nothing while the fetch still 200s) — it deserves a "please look"
# alert even though it is not a cursor fault. This threshold is deliberately
# far above ``empty_streak_threshold``: it must not re-trip the exact
# weekly/monthly feeds the honest-quiet exemption exists to protect, only the
# ones quiet long past any plausible normal cadence. Contrast with 403/5xx
# sources, which the circuit breaker already auto-pauses (source_actor) —
# this is the "polls clean, says nothing, forever" case that path can't see.
_HONEST_QUIET_STREAK_ENV = "LEGBA_SOURCE_HONEST_QUIET_STREAK_THRESHOLD"
# _DEFAULT_HONEST_QUIET_STREAK (36) lives in source_quiet_evidence beside the rule.

#: W3-F12 prep — the OLD per-source cadence check (``source_cadence_stall``)
#: is being replaced by the W3 SRC-2 staleness tripwire. Default ON (nothing
#: changes); ``=0``/``false``/``off``/``no`` skips the check entirely.
_SOURCE_CADENCE_CHECK_ENV = "LEGBA_SOURCE_CADENCE_STALL_CHECK"

# B0-12 — durable per-entity alert channels (``alert_sink_deliveries.
# channel_name``; ``sink_target`` carries the entity id). The per-analyst /
# per-source checks used to only publish to the streamless NATS subject —
# alerts EVAPORATED (no durable consumer) and a persistently-bad entity was
# re-escalated on a heartbeat (~1.2k ERROR lines/day across a dozen degraded
# sources, 2026-07-21 review). Now each check alerts on the STATE-TRANSITION
# EDGE, not the level: one durable ``state='entered'`` row (+ outward
# fan-out) when an entity enters the bad state, one ``state='recovered'`` row
# when it leaves it, silence in between. The last-alerted state is re-seeded
# from the ledger on boot so a restart cannot re-fire an ongoing condition.
ALERT_CHANNEL_GLOBAL = "liveness_stall"
ALERT_CHANNEL_ANALYST = "analyst_cadence_stall"
ALERT_CHANNEL_SOURCE = "source_cadence_stall"
ALERT_CHANNEL_SOURCE_DEGRADED = "source_degraded"
#: L1 (2026-09-20) — a DEAD LLM ROUTE is the one fault every other check on
#: this loop reports as health (the analyst still runs, the rows still land).
#: Mechanics + the incident record: :mod:`.llm_route_liveness`.
ALERT_CHANNEL_LLM_ROUTE = _llm_route.ALERT_CHANNEL_LLM_ROUTE
_TRANSITION_CHANNELS = (
    ALERT_CHANNEL_ANALYST,
    ALERT_CHANNEL_SOURCE,
    ALERT_CHANNEL_SOURCE_DEGRADED,
    ALERT_CHANNEL_LLM_ROUTE,
    _consult_health.ALERT_CHANNEL_CONSULT,  # N consecutive failed consult turns
    _llm_route.ALERT_CHANNEL_LLM_ROUTE_AUTH,  # JUDGE-2: the at-once page
    *_stale_watch.CHANNELS,  # W3 SRC-2: source_staleness_stale / _dead
    _search_watch.ALERT_CHANNEL_SEARCH_DEGRADED,  # X1/B5: degraded NOT_FOUND share
    _volume_watch.ALERT_CHANNEL_ALERT_VOLUME,  # X4: pushes per topic above the cap
    _cursor_lag.ALERT_CHANNEL_CURSOR_LAG,  # N-Z: an analyst cursor behind real time
    _stream_watch.ALERT_CHANNEL_STREAM_DOWN,  # I-4: a subscribed source's connection down
)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        return value if value > 0 else default
    except ValueError:  # pragma: no cover — defensive
        logger.warning("liveness_watchdog.bad_env %s=%r — using %s", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
        return value if value > 0 else default
    except ValueError:  # pragma: no cover — defensive
        logger.warning("liveness_watchdog.bad_env %s=%r — using %s", name, raw, default)
        return default


@dataclass
class WatchdogConfig:
    """Thresholds for the stall check (all in seconds internally)."""

    stall_after_s: float = _DEFAULT_STALL_MINUTES * 60.0
    realert_every_s: float = _DEFAULT_REALERT_MINUTES * 60.0
    check_interval_s: float = _DEFAULT_CHECK_SECONDS
    # OBS: a per-analyst run is "stale" once it exceeds factor × its cron
    # interval (floored at _CADENCE_MIN_THRESHOLD_S).
    cadence_stall_factor: float = _DEFAULT_CADENCE_FACTOR
    # OBS (D19): a source is escalated to 'degraded' once its most-recent
    # consecutive 'empty' poll-outcome run reaches this length. (B0-12: the
    # former per-episode re-alert heartbeat is gone — a continuing episode is
    # silent; alerts fire only on the entered/recovered transition edges.)
    empty_streak_threshold: int = _DEFAULT_EMPTY_STREAK
    # 2026-08-27 sweep: even a run the B0-12 discriminator classifies
    # 'honest_quiet' escalates once it reaches THIS (much higher) length —
    # see the constant's docstring. Independent of ``empty_streak_threshold``,
    # which never applies to an honest-quiet run at all.
    honest_quiet_streak_threshold: int = _DEFAULT_HONEST_QUIET_STREAK
    # W3-F12 prep: False skips the old ``source_cadence_stall`` check (the W3
    # SRC-2 staleness tripwire replaces it). Default ON.
    source_cadence_stall_check: bool = True
    # N-U: an upstream-5xx stall younger than this is recorded medium, unpaged.
    source_5xx_dead_after_s: float = _ssd.DEFAULT_SOURCE_5XX_DEAD_AFTER_HOURS * 3600.0

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        return cls(
            stall_after_s=_env_float(_STALL_AFTER_ENV, _DEFAULT_STALL_MINUTES) * 60.0,
            realert_every_s=_env_float(_REALERT_ENV, _DEFAULT_REALERT_MINUTES) * 60.0,
            check_interval_s=_env_float(_INTERVAL_ENV, _DEFAULT_CHECK_SECONDS),
            cadence_stall_factor=_env_float(_CADENCE_FACTOR_ENV, _DEFAULT_CADENCE_FACTOR),
            empty_streak_threshold=_env_int(_EMPTY_STREAK_ENV, _DEFAULT_EMPTY_STREAK),
            honest_quiet_streak_threshold=_env_int(
                _HONEST_QUIET_STREAK_ENV, _DEFAULT_HONEST_QUIET_STREAK
            ),
            source_cadence_stall_check=(
                os.getenv(_SOURCE_CADENCE_CHECK_ENV, "").strip().lower()
                not in ("0", "false", "off", "no")
            ),
            source_5xx_dead_after_s=_env_float(
                _ssd.SOURCE_5XX_DEAD_AFTER_ENV, _ssd.DEFAULT_SOURCE_5XX_DEAD_AFTER_HOURS,
            ) * 3600.0,
        )


class LivenessWatchdog:
    """Observes signal + finding traffic; alerts on a silent stall.

    Construct with a connected :class:`legba.data.nats.NatsStore`. Call
    :meth:`start` to subscribe + launch the check loop, :meth:`stop` to tear it
    down. Time is read from :func:`time.monotonic` so a wall-clock jump (NTP
    step) can't spuriously trip or mask a stall.
    """

    def __init__(
        self,
        nats_store: Any,
        config: WatchdogConfig | None = None,
        *,
        pg_store: Any = None,
        is_leader: Any = None,
        alert_sinks: Any = None,
    ) -> None:
        self._nats = nats_store
        self._cfg = config or WatchdogConfig.from_env()
        self._started_at = time.monotonic()
        self._last_signal_at: float | None = None
        self._last_finding_at: float | None = None
        self._last_alert_at: float | None = None
        self._stalled = False
        self._subs: list[Any] = []
        self._task: asyncio.Task | None = None
        # OBS — per-analyst cadence-liveness. Optional: when no pg_store is
        # wired the watchdog runs exactly as before (global stall only). The
        # ``is_leader`` callable (returns bool) gates emission so that in a
        # multi-replica deployment only the leader alerts; default-on (always
        # leader) matches the single-node rig and the global stall check, which
        # is itself not leader-gated.
        self._pg = pg_store
        self._is_leader = is_leader
        # Optional outward alert-sink dispatcher
        # (:class:`legba.data.alerts.AlertSinkDispatcher`). When wired, a
        # global-stall alert ALSO fans out through the registered sinks
        # (webhook first) so a full-pipeline stall can page an operator
        # endpoint, not just land a ledger row. Best-effort by the
        # dispatcher's never-raise contract.
        self._alert_sinks = alert_sinks
        # B0-12 — per-entity last-alerted state, channel → entity → state
        # ('entered' | 'recovered'). None until lazily seeded from the durable
        # ``alert_sink_deliveries`` ledger (so a restart cannot re-fire an
        # ongoing condition); maintained in memory afterwards — this watchdog
        # is the only writer of its channels and is leader-gated.
        self._alert_states: dict[str, dict[str, str]] | None = None

    # -- activity recording (subscription callbacks) --------------------

    async def _on_signal(self, _msg: Any) -> None:
        self._last_signal_at = time.monotonic()
        self._note_recovery("signal")

    async def _on_finding(self, _msg: Any) -> None:
        self._last_finding_at = time.monotonic()
        self._note_recovery("finding")

    def _note_recovery(self, what: str) -> None:
        if self._stalled:
            self._stalled = False
            self._last_alert_at = None
            logger.info(
                "liveness_watchdog.recovered first_activity=%s — pipeline live again",
                what,
            )

    # -- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        """Subscribe to the liveness subjects + launch the periodic check."""
        nc = self._nats.nc
        # Core (non-JetStream) subscriptions: ephemeral, no acks — they observe
        # the live subject traffic without binding a durable consumer or
        # interfering with the real planes' JetStream delivery.
        self._subs.append(await nc.subscribe(SIGNAL_WILDCARD, cb=self._on_signal))
        self._subs.append(await nc.subscribe(FINDING_WILDCARD, cb=self._on_finding))
        self._task = asyncio.create_task(self._run(), name="legba-liveness-watchdog")
        logger.info(
            "liveness_watchdog.started stall_after=%.0fs realert=%.0fs interval=%.0fs",
            self._cfg.stall_after_s,
            self._cfg.realert_every_s,
            self._cfg.check_interval_s,
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass
            self._task = None
        for sub in self._subs:
            try:
                await sub.unsubscribe()
            except Exception:  # pragma: no cover — best-effort teardown
                logger.debug("liveness_watchdog.unsubscribe failed")
        self._subs.clear()

    # -- check loop -----------------------------------------------------

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._cfg.check_interval_s)
            try:
                await self.check_once(time.monotonic())
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception as exc:  # pragma: no cover — never let the loop die
                logger.warning("liveness_watchdog.check_error err=%s", exc)
            # OBS — per-analyst cadence-liveness (independent of the global
            # check; a DB read failure must not stop the global stall loop).
            if self._pg is not None:
                try:
                    await self.check_analyst_cadence_once(time.monotonic())
                except asyncio.CancelledError:  # pragma: no cover
                    raise
                except Exception as exc:  # pragma: no cover — never kill the loop
                    logger.warning("liveness_watchdog.cadence_check_error err=%s", exc)
                # OBS — per-source cadence-liveness (DQ-H5).
                try:
                    await self.check_source_cadence_once(time.monotonic())
                except asyncio.CancelledError:  # pragma: no cover
                    raise
                except Exception as exc:  # pragma: no cover — never kill the loop
                    logger.warning("liveness_watchdog.source_check_error err=%s", exc)
                # OBS — per-source empty-200 streak escalation (D19).
                try:
                    await self.check_source_empty_streak_once(time.monotonic())
                except asyncio.CancelledError:  # pragma: no cover
                    raise
                except Exception as exc:  # pragma: no cover — never kill the loop
                    logger.warning(
                        "liveness_watchdog.empty_streak_check_error err=%s", exc
                    )
                # W3 SRC-2 — staleness tripwire (no new items for N× the
                # feed's expected interval; reads get_source_health's rows).
                try:
                    await _stale_watch.check_source_staleness_once(
                        self, time.monotonic(),
                        min_interval_s=_stale_watch.MIN_INTERVAL_S,
                    )
                except asyncio.CancelledError:  # pragma: no cover
                    raise
                except Exception as exc:  # pragma: no cover — never kill the loop
                    logger.warning("liveness_watchdog.staleness_check_error err=%s", exc)
                # L1 — dead LLM route (a model retired upstream looks like
                # health from every other check on this loop).
                try:
                    await self.check_llm_route_liveness_once(time.monotonic())
                except asyncio.CancelledError:  # pragma: no cover
                    raise
                except Exception as exc:  # pragma: no cover — never kill the loop
                    logger.warning(
                        "liveness_watchdog.llm_route_check_error err=%s", exc
                    )
                # Consult route: N consecutive failed/unfinished turns.
                try:
                    await self.check_consult_health_once(time.monotonic())
                except asyncio.CancelledError:  # pragma: no cover
                    raise
                except Exception as exc:  # pragma: no cover — never kill the loop
                    logger.warning(
                        "liveness_watchdog.consult_health_check_error err=%s", exc
                    )
                # JUDGE-2 — a HOSTED route's 401/402/403/404 pages AT ONCE.
                try:
                    await _llm_route.check_auth_failed_once(self)
                except asyncio.CancelledError:  # pragma: no cover
                    raise
                except Exception as exc:  # pragma: no cover — never kill the loop
                    logger.warning("liveness_watchdog.llm_auth_check_error err=%s", exc)
                # X1/B5 — the search plane answering degraded (never raises).
                await _search_watch.check_search_degraded_once(self)
                await _volume_watch.check_alert_volume_once(self)  # X4 (never raises)
                await _surge_watch.check_volume_surge_once(self)  # L-1 (never raises)
                await _cursor_lag.check_cursor_lag_once(self, time.monotonic())  # N-Z
                await _stream_watch.check_stream_liveness_once(  # I-4 (never raises)
                    self, time.monotonic(), min_interval_s=_stream_watch.MIN_INTERVAL_S)

    def _last_activity_at(self) -> float | None:
        candidates = [t for t in (self._last_signal_at, self._last_finding_at) if t is not None]
        return max(candidates) if candidates else None

    async def check_once(self, now: float) -> bool:
        """Evaluate the stall condition once. Returns True iff it alerted.

        Pure given ``now`` so it is unit-testable without real timers. The boot
        grace: before any activity is seen, the reference point is the
        watchdog's start time, so a cold rig only alerts once ``stall_after``
        has elapsed since boot (not immediately).
        """
        reference = self._last_activity_at() or self._started_at
        idle_s = now - reference
        if idle_s < self._cfg.stall_after_s:
            return False
        # Stalled. Rate-limit the alert to a heartbeat.
        if self._last_alert_at is not None and (now - self._last_alert_at) < self._cfg.realert_every_s:
            return False
        await self._emit_stall_alert(idle_s)
        self._stalled = True
        self._last_alert_at = now
        return True

    async def _emit_stall_alert(self, idle_s: float) -> None:
        idle_min = idle_s / 60.0
        title = f"Pipeline stall: no signal or finding for {idle_min:.0f} min"
        body = (
            "The liveness watchdog has not observed any signal "
            f"(legba.signals.>) or analyst finding (analyst.>) for "
            f"{idle_min:.1f} minutes (threshold "
            f"{self._cfg.stall_after_s / 60.0:.0f} min). The acquisition → "
            "fan-out → analysis pipeline may be wedged: check the source "
            "pollers, the trigger engine, and the analyst worker pool."
        )
        envelope = _stall_envelope(title=title, body=body, idle_seconds=idle_s)
        logger.error("liveness_watchdog.stall idle_minutes=%.1f — emitting alert", idle_min)
        await self._publish_alert(envelope)
        # B0-12 (2026-07-14): the publish above lands on the streamless
        # `legba.alerts.high` subject — NO durable consumer, so a GLOBAL stall
        # (the ~13h silent-outage class, 2026-07-14) evaporated and no operator
        # saw it. ALSO persist it as a durable alert_sink_deliveries row — the
        # operator-visible escalations panel (D1) + the W1-T3 non-delivery canary
        # both read that table — so a full-pipeline stall is LOUD, not silent.
        await self._record_stall_delivery(title, body, idle_min)
        # Outward fan-out (webhook etc.) — a stall alert that only ever
        # landed internal rows still needed a human to be LOOKING; when a sink
        # is configured this pages the operator endpoint directly.
        await self._fan_out_alert_sinks(title, body)

    async def _fan_out_alert_sinks(
        self, title: str, body: str, *, channel_name: str = ALERT_CHANNEL_GLOBAL,
        severity: str = "high", entity_id: str = "pipeline", close: bool = False,
    ) -> None:
        """L-1: raise through the ONE alert path (:mod:`legba.data.alerts.raise_alert`;
        the watchdog's channels ARE its alert kinds; entry opens the entity's
        episode, recovery closes it; the page rule decides). Guarded — the loop
        never dies on the alert edge; no dispatcher wired ⇒ raised held."""
        try:
            from ..data.alerts.raise_alert import raise_alert

            await raise_alert(
                getattr(self._pg, "pool", self._pg), kind=channel_name,
                severity=severity, summary=title, detail=body, subject_id=entity_id,
                close=close, raised_by="liveness_watchdog", dispatcher=self._alert_sinks,
                hold=None if self._alert_sinks is not None else "no alert dispatcher wired",
            )
        except Exception as exc:  # pragma: no cover — never crash the loop
            logger.warning("liveness_watchdog.alert_raise_failed err=%s", exc)

    async def _insert_delivery_row(
        self,
        *,
        channel_name: str,
        sink_target: str,
        severity: str,
        payload: dict[str, Any],
    ) -> None:
        """Persist one durable ``alert_sink_deliveries`` row (status
        ``logged_only`` — recorded, not externally delivered) so the alert
        surfaces where the operator already looks (the escalations panel +
        the W1-T3 non-delivery canary), instead of evaporating on the
        streamless NATS subject (B0-12). Fail-safe: a write error must NEVER
        crash the watchdog loop (the whole point is to observe stalls, not
        add one)."""
        if self._pg is None:
            return
        try:
            async with self._pg.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO alert_sink_deliveries (
                        channel_name, sink_kind, sink_target, severity,
                        attempt_number, status, payload_summary
                    ) VALUES ($1, $2, $3, $4, 1, $5, $6::jsonb)
                    """,
                    channel_name,
                    "liveness_watchdog",    # sink_kind
                    sink_target,
                    severity,
                    "logged_only",          # status: durable, not externally delivered
                    json.dumps(payload, separators=(",", ":"), default=str),
                )
        except Exception as exc:  # pragma: no cover — never crash the loop
            logger.warning(
                "liveness_watchdog.delivery_row_write_failed channel=%s "
                "target=%s err=%s", channel_name, sink_target, exc,
            )

    async def _record_stall_delivery(
        self, title: str, body: str, idle_min: float
    ) -> None:
        """The GLOBAL pipeline-stall durable row (B0-12, 2026-07-14)."""
        await self._insert_delivery_row(
            channel_name=ALERT_CHANNEL_GLOBAL,
            sink_target="operator",
            severity="high",
            payload={
                "kind": "pipeline_stall",
                "title": title[:200],
                "body": body[:2000],
                "idle_minutes": round(idle_min, 1),
            },
        )

    # -- B0-12: durable transition-edge alert state ---------------------

    async def _get_alert_states(self, channel: str) -> dict[str, str]:
        """The mutable entity → last-alerted-state map for ``channel``.

        Lazily seeded ONCE per process from the durable ledger (see
        :meth:`_seed_alert_states`); in-memory afterwards.
        """
        if self._alert_states is None:
            self._alert_states = await self._seed_alert_states()
        return self._alert_states.setdefault(channel, {})

    async def _seed_alert_states(self) -> dict[str, dict[str, str]]:
        """Rebuild the per-entity last-alerted state from the durable ledger.

        The newest ``liveness_watchdog`` row per (channel, entity) carries
        ``payload_summary.state`` ('entered' | 'recovered') — that IS the
        restart-safe alert state, so no separate state table is needed. A
        seed failure degrades to an EMPTY state (an ongoing condition may
        then re-fire its entry alert once — the right failure direction for
        an alerting path) and is cached so a broken DB isn't re-queried
        every check.
        """
        states: dict[str, dict[str, str]] = {ch: {} for ch in _TRANSITION_CHANNELS}
        if self._pg is None:
            return states
        try:
            async with self._pg.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT ON (channel_name, sink_target)
                           channel_name, sink_target,
                           payload_summary->>'state' AS state
                    FROM alert_sink_deliveries
                    WHERE sink_kind = 'liveness_watchdog'
                      AND channel_name = ANY($1::text[])
                      AND sink_target IS NOT NULL
                    ORDER BY channel_name, sink_target, attempted_at DESC
                    """,
                    list(_TRANSITION_CHANNELS),
                )
        except Exception as exc:
            logger.warning(
                "liveness_watchdog.alert_state_seed_failed err=%s — starting "
                "from empty state (an ongoing condition may re-alert once)",
                exc,
            )
            return states
        for r in rows:
            row = dict(r)
            channel = row.get("channel_name")
            entity = row.get("sink_target")
            state = row.get("state")
            if channel in states and entity and state in ("entered", "recovered"):
                states[channel][str(entity)] = str(state)
        return states

    async def _record_transition(
        self,
        *,
        channel: str,
        entity_id: str,
        state: str,
        severity: str,
        kind: str,
        title: str,
        body: str,
        extra: dict[str, Any] | None = None,
        fan_out: bool = True,
    ) -> None:
        """Mark an entity's alert-state transition (entry or recovery).

        In-memory FIRST (the edge gate must hold this process even when the
        durable write fails — else a broken DB turns the edge back into a
        60s-level firehose), then the durable ``alert_sink_deliveries`` row,
        then the outward fan-out. Every leg is fail-safe.
        """
        states = await self._get_alert_states(channel)
        states[entity_id] = state
        payload: dict[str, Any] = {
            "kind": kind,
            "state": state,
            "title": title[:200],
            "body": body[:2000],
        }
        if extra:
            payload.update(extra)
        await self._insert_delivery_row(
            channel_name=channel,
            sink_target=entity_id,
            severity=severity,
            payload=payload,
        )
        # L-1: every transition is raised (``fan_out`` is kept for callers; the
        # page rule, not the producer, decides — a transient stall's medium
        # severity is under the rule's ``high`` for these kinds).
        await self._fan_out_alert_sinks(
            title, body, channel_name=channel, severity=severity,
            entity_id=entity_id, close=state == "recovered",
        )

    async def _emit_recoveries(
        self,
        *,
        channel: str,
        kind: str,
        noun: str,
        still_bad: set[str],
    ) -> None:
        """Fire the recovery edge for every entity durably 'entered' on
        ``channel`` that is no longer in the bad set — once per episode.

        Severity 'info' (a closed episode is information, not a page); no
        streamless NATS publish — the durable row + outward fan-out are the
        operator-visible surfaces.
        """
        states = await self._get_alert_states(channel)
        for entity_id, state in list(states.items()):
            if state not in ("entered", _ssd.TRANSIENT_ENTERED_STATE) or entity_id in still_bad:
                continue
            title = f"{noun} recovered: {entity_id}"
            body = (
                f"{noun} '{entity_id}' has cleared the condition behind its "
                f"'{channel}' alert — the episode is closed. A future "
                "recurrence will alert anew (transition-edge alerting: one "
                "alert on entry, one on recovery, silence in between)."
            )
            logger.info(
                "liveness_watchdog.recovered channel=%s entity=%s",
                channel, entity_id,
            )
            await self._record_transition(
                channel=channel,
                entity_id=entity_id,
                state="recovered",
                severity="info",
                kind=kind,
                title=title,
                body=body,
            )

    async def _publish_alert(self, envelope: dict[str, Any]) -> None:
        """Publish an alert envelope on the streamless alert subject.

        Core publish — ``legba.alerts.*`` has NO JetStream stream, so
        ``publish_json`` (which awaits a stream ack) raised
        NoStreamResponseError and silently dropped every watchdog alert.
        """
        try:
            await self._nats.publish_core(
                STALL_ALERT_SUBJECT,
                json.dumps(envelope, separators=(",", ":")).encode("utf-8"),
            )
        except Exception as exc:  # pragma: no cover — fail loud, don't crash loop
            logger.error("liveness_watchdog.alert_publish_failed err=%s", exc)

    # -- OBS: per-analyst cadence-liveness ------------------------------

    async def check_analyst_cadence_once(self, now_monotonic: float) -> list[str]:
        """Alert on any ACTIVE cadence analyst whose newest successful run is
        older than ``factor`` × its own cron interval. Returns the alerted ids.

        The global stall check can't see one analyst going dark while the
        aggregate pipeline still flows; this closes that gap. Leader-gated (so
        only one replica alerts) and boot-graced (no alert until analysts have
        had a chance to run after a cold start).

        B0-12 transition-edge alerting: an analyst entering the stale state
        fires ONCE (streamless NATS + durable ``entered`` row + outward
        fan-out, severity high); the CONTINUING condition is silent; leaving
        the stale state fires ONE ``recovered`` row (severity info). The
        last-alerted state survives restarts via the durable ledger.
        """
        if self._pg is None:
            return []
        if self._is_leader is not None and not self._is_leader():
            return []
        # Boot grace: a freshly-booted analyst hasn't necessarily run yet.
        if (now_monotonic - self._started_at) < self._cfg.stall_after_s:
            return []
        rows = await self._fetch_cadence_rows()
        now_wall = datetime.now(tz=timezone.utc)
        stale = _evaluate_cadence_staleness(
            rows,
            now=now_wall,
            factor=self._cfg.cadence_stall_factor,
            min_threshold_s=_CADENCE_MIN_THRESHOLD_S,
        )
        # R2-FIX: a BUDGET PAUSE is not a stall. See the banner at
        # ``_BUDGET_PAUSE_GRACE_ENV`` for why this cannot be read off the
        # actor record (it lives in the Dapr state store, not Postgres) and why
        # the suppression is bounded to one cooldown window.
        paused = await self._fetch_budget_pauses()
        kept: list[tuple[str, float, float]] = []
        for analyst_id, age_s, threshold_s in stale:
            since = paused.get(analyst_id)
            if since is not None:
                logger.info(
                    "liveness_watchdog.cadence_stall_suppressed analyst=%s "
                    "age_hours=%.1f reason=budget_pause demoted_s_ago=%.0f — "
                    "the analyst is silent because its per-day token bucket is "
                    "exhausted and the actor is on a BUDGET_THROTTLED cooldown, "
                    "which is the budget working; it will alert if it is still "
                    "silent once the cooldown window lapses",
                    analyst_id, age_s / 3600.0, since,
                )
                continue
            kept.append((analyst_id, age_s, threshold_s))
        stale = kept

        states = await self._get_alert_states(ALERT_CHANNEL_ANALYST)
        alerted: list[str] = []
        for analyst_id, age_s, threshold_s in stale:
            if states.get(analyst_id) == "entered":
                continue  # ongoing condition — the entry edge already fired
            await self._emit_cadence_alert(analyst_id, age_s, threshold_s)
            alerted.append(analyst_id)
        await self._emit_recoveries(
            channel=ALERT_CHANNEL_ANALYST,
            kind="analyst_cadence_recovered",
            noun="Analyst",
            still_bad={aid for aid, _, _ in stale},
        )
        return alerted

    async def _fetch_budget_pauses(self) -> dict[str, float]:
        """Analysts whose newest budget demotion is inside the pause window.

        ``{analyst_id: seconds_since_the_demotion}``. Empty on any failure —
        this gate may only ever SUPPRESS an alert, so a broken read must
        degrade to alerting rather than to silence.
        """
        grace = _env_float(
            _BUDGET_PAUSE_GRACE_ENV, _DEFAULT_BUDGET_PAUSE_GRACE_S
        )
        if grace <= 0 or self._pg is None:
            return {}
        try:
            async with self._pg.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT analyst_id,
                           extract(epoch FROM (now() - max(occurred_at)))
                               AS since_s
                      FROM budget_demotion_events
                     WHERE occurred_at > now() - ($1 || ' seconds')::interval
                     GROUP BY 1
                    """,
                    str(int(grace)),
                )
        except Exception as exc:  # noqa: BLE001 — never suppress on a failure
            logger.warning(
                "liveness_watchdog.budget_pause_lookup_failed err=%s — cadence "
                "stalls alert normally this pass", exc,
            )
            return {}
        return {
            str(row["analyst_id"]): float(row["since_s"] or 0.0)
            for row in rows
        }

    async def _fetch_cadence_rows(self) -> list[dict[str, Any]]:
        """The active cadence-bearing analysts + their newest successful run.

        On-demand kinds (consult / deep_consult) carry no ``fallback_schedule``
        and are excluded by the non-empty-cron filter. Analysts that have never
        run (``last_run`` NULL — a fresh registration) are filtered downstream
        in the evaluator, so a not-yet-fired analyst can't false-alarm.
        """
        async with self._pg.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT d.body->'identity'->>'id'               AS analyst_id,
                       d.body->'cadence'->>'fallback_schedule'  AS cron,
                       max(t.run_started_at)                    AS last_run,
                       extract(epoch FROM max(t.run_ended_at - t.run_started_at) FILTER (WHERE t.run_started_at > now() - interval '1 day')) AS recent_run_s  -- in-flight grace (cadence_staleness)
                FROM analyst_descriptors d
                LEFT JOIN analyst_traces t
                       ON t.analyst_id = d.body->'identity'->>'id'
                      AND t.status = 'success'
                WHERE d.is_head AND d.state = 'active'
                  AND coalesce(d.body->'cadence'->>'fallback_schedule', '') <> ''
                GROUP BY 1, 2
                """
            )
        return [dict(r) for r in rows]

    async def _emit_cadence_alert(
        self, analyst_id: str, age_s: float, threshold_s: float
    ) -> None:
        age_h = age_s / 3600.0
        thr_h = threshold_s / 3600.0
        title = f"Analyst cadence stall: {analyst_id} silent {age_h:.1f}h"
        body = (
            f"Analyst '{analyst_id}' is ACTIVE but its newest successful run is "
            f"{age_h:.1f}h old — past its staleness threshold of {thr_h:.1f}h "
            f"({self._cfg.cadence_stall_factor:g}× its cron interval, measured "
            "along its schedule from the last run, so a windowed cron's quiet "
            "hours are never a stall). The "
            "aggregate pipeline may look healthy (other analysts still "
            "publishing), so the global stall check won't catch this. Check "
            "this analyst's actor activation, descriptor head version, deps "
            "resolution, and the registry /typed deserialization for its kind. "
            "A budget pause inside the last hour is already ruled out — this "
            "check suppresses those, and says so in the log when it does."
        )
        envelope = _cadence_envelope(
            analyst_id=analyst_id, title=title, body=body, age_seconds=age_s
        )
        logger.error(
            "liveness_watchdog.cadence_stall analyst=%s age_hours=%.1f threshold_hours=%.1f"
            " — emitting alert",
            analyst_id,
            age_h,
            thr_h,
        )
        await self._publish_alert(envelope)
        # B0-12: durable entry edge + outward fan-out.
        await self._record_transition(
            channel=ALERT_CHANNEL_ANALYST,
            entity_id=analyst_id,
            state="entered",
            severity="high",
            kind="analyst_cadence_stall",
            title=title,
            body=body,
            extra={"age_hours": round(age_h, 1)},
        )

    # -- L1: dead LLM route (2026-09-20) --------------------------------

    async def check_llm_route_liveness_once(
        self, now_monotonic: float
    ) -> list[str]:
        """Alert on any BUSY LLM stack component that is answering nothing.

        Returns the alerted component ids. Thresholds, the receipts read and
        the verdict live in :mod:`.llm_route_liveness` (which carries the full
        account of the 2.7-day silent grader outage this closes); the watchdog
        owns the leader gate, the boot grace and the transition-edge ledger.
        """
        if self._pg is None:
            return []
        if self._is_leader is not None and not self._is_leader():
            return []
        # Boot grace: a thin window of receipts is not a verdict on a route.
        if (now_monotonic - self._started_at) < self._cfg.stall_after_s:
            return []
        dead = await _llm_route.dead_routes(self._pg)
        states = await self._get_alert_states(ALERT_CHANNEL_LLM_ROUTE)
        alerted: list[str] = []
        for route in dead:
            if states.get(route["component_id"]) == "entered":
                continue  # ongoing condition — the entry edge already fired
            await self._emit_llm_route_alert(route)
            alerted.append(route["component_id"])
        await self._emit_recoveries(
            channel=ALERT_CHANNEL_LLM_ROUTE,
            kind="llm_route_recovered",
            noun="LLM route",
            still_bad={r["component_id"] for r in dead},
        )
        return alerted

    async def check_consult_health_once(self, now_monotonic: float) -> list[str]:
        """N consecutive failed/unfinished consult turns alert (body in
        :mod:`.consult_health`: leader-gated, boot-graced, transition-edge)."""
        return await _consult_health.check_once(self, now_monotonic)

    async def _emit_llm_route_alert(self, route: dict[str, Any]) -> None:
        _llm_route.log_dead_route(route)
        await self._record_transition(**_llm_route.transition_kwargs(route))

    # -- OBS: per-source cadence-liveness (DQ-H5) -----------------------

    async def check_source_cadence_once(self, now_monotonic: float) -> list[str]:
        """Alert on any ACTIVE poll source whose newest signal is older than
        ``factor`` × its own poll-cron interval. Returns the alerted source ids.

        The sweep found 10 active sources silent >7d while the aggregate signal
        wildcard never went quiet (other sources kept publishing) — the poll
        fired HTTP-200, wrote 0 signals + 0 error rows, and nothing noticed.
        Mirrors the per-analyst check: leader-gated, boot-graced,
        transition-edge alerted (entry + recovery only — B0-12).

        B0-12 precision: a cadence-stale source whose freshest poll evidence
        shows the FEED ITSELF is quiet (clean empty poll, newest observed
        upstream entry <= our newest ingested signal) is a slow news day —
        weekly/monthly feeds were 7/8 of the B0-11 false-positive cluster —
        and stays SILENT (it also counts as recovered for a previously-fired
        stall alert).
        """
        if not self._cfg.source_cadence_stall_check:
            return []  # LEGBA_SOURCE_CADENCE_STALL_CHECK off (W3-F12 prep)
        if self._pg is None:
            return []
        if self._is_leader is not None and not self._is_leader():
            return []
        if (now_monotonic - self._started_at) < self._cfg.stall_after_s:
            return []
        rows = await self._fetch_source_cadence_rows()
        rows_by_id = {r["source_id"]: r for r in rows}
        now_wall = datetime.now(tz=timezone.utc)
        stale = _evaluate_cadence_staleness(
            rows,
            now=now_wall,
            factor=self._cfg.cadence_stall_factor,
            min_threshold_s=_SOURCE_CADENCE_MIN_THRESHOLD_S,
            id_key="source_id",
            ts_key="last_signal",
        )
        # Honest-quiet discriminator gate (no alert; see docstring).
        stale = [
            (source_id, age_s, threshold_s)
            for source_id, age_s, threshold_s in stale
            if not _source_stall_is_honest_quiet(rows_by_id.get(source_id) or {})
        ]
        states = await self._get_alert_states(ALERT_CHANNEL_SOURCE)
        alerted: list[str] = []
        for source_id, age_s, threshold_s in stale:
            row = rows_by_id.get(source_id) or {}
            transient = _ssd.is_transient_upstream_5xx(
                row, age_s, self._cfg.source_5xx_dead_after_s)
            state = states.get(source_id)
            if state == "entered" or (state == _ssd.TRANSIENT_ENTERED_STATE and transient):
                continue  # ongoing condition — the entry edge already fired
            await self._emit_source_cadence_alert(
                source_id, age_s, threshold_s, row, transient=transient,
            )
            alerted.append(source_id)
        await self._emit_recoveries(
            channel=ALERT_CHANNEL_SOURCE,
            kind="source_cadence_recovered",
            noun="Source",
            still_bad={sid for sid, _, _ in stale},
        )
        return alerted

    async def _fetch_source_cadence_rows(self) -> list[dict[str, Any]]:
        """Active poll sources + their poll cron + newest signal timestamp +
        the source's NEWEST non-productive-poll outcome (DQ-H5b #88).

        The poll cron lives at ``<acquisition>.schedule.raw`` in the source
        descriptor body (a ``{raw, ui_hint}`` object); ``$.**.schedule.raw``
        extracts it regardless of the exact nesting. A source that has NEVER
        produced (``last_signal`` NULL — e.g. a fresh registration) is skipped
        downstream by the evaluator, so it can't false-alarm.

        The LATERAL join surfaces WHY a source is silent: the most recent
        ``source_poll_outcomes`` row (one per poll since migration 0114) tells
        the alert whether the feed errored (``last_poll_outcome='error'`` +
        ``last_poll_health`` / ``last_poll_error``), simply returned nothing
        ('empty' / 'healthy'), or last polled PRODUCTIVELY ('success' +
        ``last_poll_signals_written``) — or whether NO poll outcome was
        recorded at all, which now means literally what the diagnosis always
        claimed: the poll never ran. Left join so it degrades to NULLs when the
        provenance table is empty.
        """
        async with self._pg.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT d.descriptor_id                                  AS source_id,
                       jsonb_path_query_first(d.body, '$.**.schedule.raw') #>> '{}'
                                                                        AS cron,
                       max(s.fetched_at)                                AS last_signal,
                       po.outcome                                       AS last_poll_outcome,
                       po.health_state                                  AS last_poll_health,
                       po.error                                         AS last_poll_error,
                       po.occurred_at                                   AS last_poll_at,
                       po.newest_entry_ts                               AS last_poll_newest_entry_ts,
                       po.signals_written                               AS last_poll_signals_written,
                       po.keyword_filtered                              AS last_poll_keyword_filtered
                FROM source_descriptors d
                LEFT JOIN signals s ON s.source_id = d.descriptor_id
                LEFT JOIN LATERAL (
                    SELECT outcome, health_state, error, occurred_at,
                           newest_entry_ts, signals_written, keyword_filtered
                    FROM source_poll_outcomes
                    WHERE source_id = d.descriptor_id
                    ORDER BY occurred_at DESC
                    LIMIT 1
                ) po ON TRUE
                WHERE d.is_head AND d.state = 'active'
                GROUP BY 1, 2, po.outcome, po.health_state, po.error,
                         po.occurred_at, po.newest_entry_ts,
                         po.signals_written, po.keyword_filtered
                """
            )
        return [dict(r) for r in rows]

    async def _emit_source_cadence_alert(
        self,
        source_id: str,
        age_s: float,
        threshold_s: float,
        row: dict[str, Any] | None = None,
        *,
        transient: bool = False,
    ) -> None:
        """``transient`` (N-U): an intermittent upstream 5xx — the edge is
        RECORDED at medium (ledger row) but not published or fanned out."""
        age_h = age_s / 3600.0
        thr_h = threshold_s / 3600.0
        title = f"Source poll stall: {source_id} silent {age_h:.1f}h"
        body = (
            f"Source '{source_id}' is ACTIVE but its newest signal is "
            f"{age_h:.1f}h old — past its staleness threshold of {thr_h:.1f}h "
            f"({self._cfg.cadence_stall_factor:g}× its poll cron). "
            + _source_stall_diagnosis(row or {})
        )
        envelope = _cadence_envelope(
            analyst_id=source_id, title=title, body=body, age_seconds=age_s
        )
        # Re-tag so subscribers can distinguish a source stall from an analyst one.
        envelope["tags"] = [
            "watchdog", "liveness", "source_stall", source_id,
        ]
        envelope["stale_source_id"] = source_id
        logger.log(
            logging.WARNING if transient else logging.ERROR,
            "liveness_watchdog.source_stall source=%s age_hours=%.1f "
            "threshold_hours=%.1f transient_5xx=%s — %s",
            source_id, age_h, thr_h, transient,
            "recording, not paging" if transient else "emitting alert",
        )
        if not transient:
            await self._publish_alert(envelope)
        # B0-12: durable entry edge + outward fan-out (none when transient).
        await self._record_transition(
            channel=ALERT_CHANNEL_SOURCE,
            entity_id=source_id,
            state=_ssd.TRANSIENT_ENTERED_STATE if transient else "entered",
            severity="medium" if transient else "high",
            kind="source_cadence_stall",
            title=title,
            body=body,
            extra={"age_hours": round(age_h, 1), "transient_5xx": transient},
            fan_out=not transient,
        )

    # -- OBS: per-source empty-200 streak escalation (D19) ---------------

    async def check_source_empty_streak_once(self, now_monotonic: float) -> list[str]:
        """Escalate any source whose most-recent CONSECUTIVE 'empty' poll-outcome
        run has reached ``empty_streak_threshold`` — unless the run is
        classified honest-quiet, in which case it instead escalates once it
        reaches the much higher ``honest_quiet_streak_threshold``.

        This catches the xinhua/aljazeera dead-15d class: a feed that keeps
        firing HTTP-200-with-0-items, logs every poll health='healthy', and so
        never registers as an error — yet is functionally dead. The cadence
        check eventually trips on signal staleness, but for a 6-hourly source
        that is many hours later; the empty-streak run reaches the threshold
        much sooner.

        B0-12 precision: each empty poll-outcome row may carry the handler's
        ``newest_entry_ts`` observation, which splits the qualifying streaks
        (see :func:`_evaluate_empty_streaks`) into honest-quiet (feed simply
        has nothing newer than our last ingest — silent below
        ``honest_quiet_streak_threshold``, escalated at 'degraded' past it;
        this was the B0-11 false-quiet class), cursor_fault (feed CARRIES
        newer entries yet the polls yield 0 — the cursor/filter is eating
        live content → escalate high), and unknown (no evidence → the
        pre-existing degraded escalation). Leader-gated, boot-graced,
        transition-edge alerted (entry + recovery only; a continuing episode
        is silent).

        2026-08-27 sweep: source.wto.news ran 110 consecutive empty/healthy
        polls (9 days) that were *correctly* evidenced honest-quiet on every
        one — so under the pre-fix rule it never escalated at all, at any
        length. ``honest_quiet_streak_threshold`` is what makes a streak that
        long escalate anyway.

        2026-08-29 sweep (finding 1): that fix shipped structurally dead —
        the fetch below is bounded by ``_empty_streak_fetch_window()``, sized
        off the configured thresholds so it always strictly exceeds them (see
        that method's docstring); the old fixed 20-row window could never
        report a streak >= the default 36-length ``honest_quiet_threshold``.
        """
        if self._pg is None:
            return []
        if self._is_leader is not None and not self._is_leader():
            return []
        if (now_monotonic - self._started_at) < self._cfg.stall_after_s:
            return []
        rows = await self._fetch_source_empty_streak_rows(
            window=self._empty_streak_fetch_window()
        )
        degraded = _evaluate_empty_streaks(
            rows,
            threshold=self._cfg.empty_streak_threshold,
            honest_quiet_threshold=self._cfg.honest_quiet_streak_threshold,
        )
        states = await self._get_alert_states(ALERT_CHANNEL_SOURCE_DEGRADED)
        alerted: list[str] = []
        for source_id, streak, latest_at, fault_class in degraded:
            if states.get(source_id) == "entered":
                continue  # ongoing episode — the entry edge already fired
            await self._emit_empty_streak_alert(
                source_id, streak, latest_at, fault_class,
            )
            alerted.append(source_id)
        # Recovery edge: a previously-alerted source that is no longer in the
        # degraded set (producing again, streak broken, or reclassified
        # honest-quiet) closes its episode — a re-degradation alerts anew.
        await self._emit_recoveries(
            channel=ALERT_CHANNEL_SOURCE_DEGRADED,
            kind="source_degraded_recovered",
            noun="Source",
            still_bad={sid for sid, _, _, _ in degraded},
        )
        return alerted

    def _empty_streak_fetch_window(self) -> int:
        """The per-source row LIMIT for the empty-streak fetch.

        2026-08-29 DQ sweep, finding 1: must strictly exceed BOTH
        ``empty_streak_threshold`` and ``honest_quiet_streak_threshold`` — a
        window at or below a threshold makes that threshold structurally
        unreachable, no matter how long the real streak runs, because
        :func:`_evaluate_empty_streaks` can only ever see as many leading
        rows as it was handed. Computed off the CONFIGURED values (not the
        shipped defaults) so raising either threshold via its env knob can't
        silently reopen the gap. Mirrors
        ``legba.data.analysts.deterministic_handlers.entity_gc._SOURCE_STREAK_WINDOW``
        (``max(threshold + slack, floor)``), generalized to two independent
        thresholds. ``_EMPTY_STREAK_WINDOW_SLACK`` rows of margin beyond the
        higher threshold is enough to also observe the breaking (non-empty)
        row when the true streak is close to the bound; a streak longer than
        the resulting window is reported at the window's length, which still
        clears the threshold and still escalates (a deliberate, cheaper
        alternative to an unbounded per-source scan — see the constant's
        docstring).
        """
        return max(
            _EMPTY_STREAK_WINDOW,
            self._cfg.empty_streak_threshold + _EMPTY_STREAK_WINDOW_SLACK,
            self._cfg.honest_quiet_streak_threshold + _EMPTY_STREAK_WINDOW_SLACK,
        )

    async def _fetch_source_empty_streak_rows(self, *, window: int) -> list[dict[str, Any]]:
        """The recent poll-outcome run per ACTIVE source, newest-first, plus the
        source's newest produced-signal timestamp.

        Pulls the last ``window`` ``source_poll_outcomes`` rows for each active
        source — see :meth:`_empty_streak_fetch_window` for how the caller
        sizes it (2026-08-29: it must strictly exceed the configured
        thresholds, not just the ``_EMPTY_STREAK_WINDOW`` floor). Since
        migration 0114 a PRODUCTIVE poll writes an ``outcome='success'`` row,
        which breaks the empty run directly. Every row STILL carries
        ``last_signal`` (``max(signals.fetched_at)`` for the source):
        ``_evaluate_empty_streaks`` uses it to RESET a streak whose
        most-recent empty poll is OLDER than a produced signal (the source is
        alive again) — the only thing that could see a recovery across the
        pre-0114 rows, which remain on disk and where an actively-producing
        source's last recorded outcomes really are all 'empty'. Each output row
        is one outcome: ``{source_id, outcome, occurred_at, health_state,
        newest_entry_ts, last_signal}`` — ``newest_entry_ts`` (B0-12) is the
        handler's newest-observed-upstream-entry evidence the discriminator
        keys on (NULL for handlers that don't record it). A head declaring
        ``cadence.expect_quiet`` (N-U: a feed quiet by nature) is excluded.
        """
        # ``window`` is caller-computed from trusted config ints (not user
        # input); inlined into the LATERAL LIMIT.
        window = int(window)
        async with self._pg.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT po.source_id        AS source_id,
                       po.outcome          AS outcome,
                       po.occurred_at      AS occurred_at,
                       po.health_state     AS health_state,
                       po.newest_entry_ts  AS newest_entry_ts,
                       d.body #>> '{{cadence,quiet_page_after_hours}}' AS quiet_page_after_hours,
                       (SELECT max(s.fetched_at)
                          FROM signals s
                         WHERE s.source_id = d.descriptor_id) AS last_signal
                FROM source_descriptors d
                JOIN LATERAL (
                    SELECT source_id, outcome, occurred_at, health_state,
                           newest_entry_ts
                    FROM source_poll_outcomes
                    WHERE source_id = d.descriptor_id
                    ORDER BY occurred_at DESC
                    LIMIT {window}
                ) po ON TRUE
                WHERE d.is_head AND d.state = 'active'
                  AND COALESCE(d.body #>> '{{cadence,expect_quiet}}', 'false') <> 'true'
                ORDER BY po.source_id, po.occurred_at DESC
                """
            )
        return [dict(r) for r in rows]

    async def _emit_empty_streak_alert(
        self,
        source_id: str,
        streak: int,
        latest_at: Any,
        fault_class: str = "unknown",
    ) -> None:
        when_s = ""
        if isinstance(latest_at, datetime):
            when_s = (
                " (most recent empty poll "
                f"{latest_at.astimezone(timezone.utc).isoformat(timespec='seconds')})"
            )
        if fault_class == "cursor_fault":
            # B0-12: the discriminator PROVED the feed carries entries newer
            # than our newest ingested signal while every poll yields 0 — the
            # cursor / since-filter / ingestion filter is eating live content
            # (the cursor-poison class). This is the escalation
            # the old check could not distinguish from a slow news day.
            title = (
                f"Source cursor/filter fault: {source_id} — upstream has newer "
                f"entries but {streak} consecutive polls yielded 0"
            )
            body = (
                f"Source '{source_id}' has returned {streak} consecutive empty "
                f"polls (HTTP-200 but 0 new signals){when_s}, and the handler "
                "OBSERVED entries in the feed NEWER than this source's newest "
                "ingested signal — upstream is publishing, but the pipeline is "
                "discarding it. This is a cursor/filter fault, not a quiet "
                "feed (the cursor-poison class): check the "
                "stored cursor timestamp (future-skew poison), the since-"
                "filter, and the ingestion/dedupe filters for this source."
            )
            tag = "cursor_fault"
            kind = "source_cursor_fault"
        elif fault_class == "honest_quiet_prolonged":
            # 2026-08-27 sweep (source.wto.news — 110 consecutive
            # empty/healthy polls, 9 days, every one honestly evidenced
            # quiet). The discriminator's own evidence says this is NOT a
            # cursor/filter fault — the source's crawl really has observed
            # nothing newer than what's already ingested — but a quiet spell
            # this long is no longer distinguishable, operationally, from the
            # source having quietly died. Worded differently from the plain
            # 'unknown' case so an operator doesn't go looking for a
            # cursor/filter bug that the evidence already rules out.
            title = (
                f"Source prolonged silence: {source_id} — {streak} consecutive "
                "empty polls, no new upstream content ever observed"
            )
            body = (
                f"Source '{source_id}' has returned {streak} consecutive empty "
                f"polls (HTTP-200, 0 new signals){when_s}, at/above the "
                f"honest-quiet escalation threshold of "
                f"{self._cfg.honest_quiet_streak_threshold}. On every one of "
                "those polls the handler's own crawl found nothing newer than "
                "what is already ingested — by the discriminator's "
                "evidence this is an honestly quiet feed, NOT a cursor/filter "
                "fault, so do not go looking for one. But a streak this long "
                "stops reading as an ordinary quiet week: the source may have "
                "moved, been retired, or changed its page structure so the "
                "parser silently finds nothing while the fetch still 200s. "
                "Health state is escalated to DEGRADED: re-probe the feed by "
                "hand before trusting its continued 'healthy' poll outcomes."
            )
            tag = "honest_quiet_prolonged"
            kind = "source_honest_quiet_prolonged"
        else:
            title = f"Source degraded: {source_id} — {streak} consecutive empty polls"
            body = (
                f"Source '{source_id}' has returned {streak} consecutive empty polls "
                f"(HTTP-200 but 0 new signals){when_s}, at/above the degraded "
                f"threshold of {self._cfg.empty_streak_threshold}. Each poll logged "
                "health='healthy', so it never registered as an error, but a feed "
                "that keeps fetching cleanly and producing nothing is functionally "
                "DEAD (the xinhua/aljazeera dead-15d class). Health state is "
                "escalated to DEGRADED: re-probe the feed URL/cursor/filter — the "
                "endpoint may have moved, the cursor may be over-trimming, or the "
                "feed may genuinely be retired. Pause or repoint the source."
            )
            tag = "source_degraded"
            kind = "source_empty_degraded"
        envelope = _cadence_envelope(
            analyst_id=source_id, title=title, body=body, age_seconds=0.0
        )
        # Re-tag + carry the escalated health so subscribers can distinguish an
        # empty-streak degradation from a hard cadence/source stall.
        envelope["tags"] = [
            "watchdog", "liveness", tag, "empty_streak", source_id,
        ]
        envelope["stale_source_id"] = source_id
        envelope["health_state"] = "degraded"
        envelope["empty_streak"] = int(streak)
        envelope["fault_class"] = fault_class
        # age_seconds isn't meaningful for a streak escalation — drop the field
        # so it isn't misread as a 0-second-old signal.
        envelope.pop("age_seconds", None)
        applicable_threshold = (
            self._cfg.honest_quiet_streak_threshold
            if fault_class == "honest_quiet_prolonged"
            else self._cfg.empty_streak_threshold
        )
        logger.error(
            "liveness_watchdog.source_degraded source=%s empty_streak=%d "
            "threshold=%d fault_class=%s — escalating",
            source_id, streak, applicable_threshold, fault_class,
        )
        await self._publish_alert(envelope)
        # B0-12: durable entry edge + outward fan-out.
        await self._record_transition(
            channel=ALERT_CHANNEL_SOURCE_DEGRADED,
            entity_id=source_id,
            state="entered",
            severity="high",
            kind=kind,
            title=title,
            body=body,
            extra={"empty_streak": int(streak), "fault_class": fault_class},
        )


def _cadence_envelope(
    *, analyst_id: str, title: str, body: str, age_seconds: float
) -> dict[str, Any]:
    """Per-analyst cadence-stall alert envelope (same shape as _stall_envelope
    so existing alert subscribers render it identically)."""
    return {
        "kind": "alert",
        "severity": "high",
        "title": title,
        "body": body,
        "confidence": 1.0,
        "tags": ["watchdog", "liveness", "cadence_stall", analyst_id],
        "evidence": [],
        "routing_hint": "ops",
        "analyst_id": "system.liveness_watchdog",
        "analyst_version": None,
        "target_id": None,
        "target_version": None,
        "run_id": None,
        "stale_analyst_id": analyst_id,
        "age_seconds": round(age_seconds, 1),
        "emitted_at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _stall_envelope(*, title: str, body: str, idle_seconds: float) -> dict[str, Any]:
    """Alert envelope matching the ``alert`` output kind's NATS shape.

    Mirrors ``legba.data.outputs.alert_sinks.nats._envelope`` so existing
    subscribers (UI live feed, operator sinks) render it identically. The
    analyst/target/run fields are null — this is a system-emitted alert, not an
    analyst output.
    """
    return {
        "kind": "alert",
        "severity": "high",
        "title": title,
        "body": body,
        "confidence": 1.0,
        "tags": ["watchdog", "liveness", "stall"],
        "evidence": [],
        "routing_hint": "ops",
        "analyst_id": "system.liveness_watchdog",
        "analyst_version": None,
        "target_id": None,
        "target_version": None,
        "run_id": None,
        "idle_seconds": round(idle_seconds, 1),
        "emitted_at": datetime.now(tz=timezone.utc).isoformat(),
    }
