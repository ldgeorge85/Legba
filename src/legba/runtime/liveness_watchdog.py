# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Silent-stall liveness watchdog (resilience-observability W-1b §2).

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

from .dapr_cron import cron_to_reminder_timing

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

# OBS — per-analyst cadence-liveness (W-1b §2 extension). The GLOBAL stall
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

# OBS — per-SOURCE cadence-liveness (DQ-H5). Sources poll on their own crons
# (hourly … 6-hourly); the sweep found 10 active sources silent >7d (feeds
# HTTP-200 but the runtime poll wrote 0 signals + 0 error rows — invisible).
# Same factor × interval rule, but a higher floor so a single missed poll on a
# 6-hourly source can't false-alarm.
_SOURCE_CADENCE_MIN_THRESHOLD_S = 3.0 * 3600.0


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

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        return cls(
            stall_after_s=_env_float(_STALL_AFTER_ENV, _DEFAULT_STALL_MINUTES) * 60.0,
            realert_every_s=_env_float(_REALERT_ENV, _DEFAULT_REALERT_MINUTES) * 60.0,
            check_interval_s=_env_float(_INTERVAL_ENV, _DEFAULT_CHECK_SECONDS),
            cadence_stall_factor=_env_float(_CADENCE_FACTOR_ENV, _DEFAULT_CADENCE_FACTOR),
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
        # Per-analyst alert rate-limit, keyed by analyst id → monotonic ts.
        self._last_cadence_alert_at: dict[str, float] = {}
        # Per-source alert rate-limit (DQ-H5), keyed by source id → monotonic ts.
        self._last_source_alert_at: dict[str, float] = {}

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
        only one replica alerts), boot-graced (no alert until analysts have had
        a chance to run after a cold start), and rate-limited per analyst.
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
        alerted: list[str] = []
        for analyst_id, age_s, threshold_s in stale:
            last = self._last_cadence_alert_at.get(analyst_id)
            if last is not None and (now_monotonic - last) < self._cfg.realert_every_s:
                continue
            await self._emit_cadence_alert(analyst_id, age_s, threshold_s)
            self._last_cadence_alert_at[analyst_id] = now_monotonic
            alerted.append(analyst_id)
        return alerted

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
                       max(t.run_started_at)                    AS last_run
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
            f"({self._cfg.cadence_stall_factor:g}× its cron interval). The "
            "aggregate pipeline may look healthy (other analysts still "
            "publishing), so the global stall check won't catch this. Check "
            "this analyst's actor activation, descriptor head version, deps "
            "resolution, and the registry /typed deserialization for its kind."
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

    # -- OBS: per-source cadence-liveness (DQ-H5) -----------------------

    async def check_source_cadence_once(self, now_monotonic: float) -> list[str]:
        """Alert on any ACTIVE poll source whose newest signal is older than
        ``factor`` × its own poll-cron interval. Returns the alerted source ids.

        The sweep found 10 active sources silent >7d while the aggregate signal
        wildcard never went quiet (other sources kept publishing) — the poll
        fired HTTP-200, wrote 0 signals + 0 error rows, and nothing noticed.
        Mirrors the per-analyst check: leader-gated, boot-graced, rate-limited.
        """
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
        alerted: list[str] = []
        for source_id, age_s, threshold_s in stale:
            last = self._last_source_alert_at.get(source_id)
            if last is not None and (now_monotonic - last) < self._cfg.realert_every_s:
                continue
            await self._emit_source_cadence_alert(
                source_id, age_s, threshold_s, rows_by_id.get(source_id) or {},
            )
            self._last_source_alert_at[source_id] = now_monotonic
            alerted.append(source_id)
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
        ``source_poll_outcomes`` row (written only for empty/error polls) tells
        the alert whether the feed errored (``last_poll_outcome='error'`` +
        ``last_poll_health`` / ``last_poll_error``) or simply returned nothing
        ('empty' / 'healthy') — or whether NO poll outcome was recorded at all
        (poll reminder likely dead). Left join so it degrades to NULLs when the
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
                       po.occurred_at                                   AS last_poll_at
                FROM source_descriptors d
                LEFT JOIN signals s ON s.source_id = d.descriptor_id
                LEFT JOIN LATERAL (
                    SELECT outcome, health_state, error, occurred_at
                    FROM source_poll_outcomes
                    WHERE source_id = d.descriptor_id
                    ORDER BY occurred_at DESC
                    LIMIT 1
                ) po ON TRUE
                WHERE d.is_head AND d.state = 'active'
                GROUP BY 1, 2, po.outcome, po.health_state, po.error, po.occurred_at
                """
            )
        return [dict(r) for r in rows]

    async def _emit_source_cadence_alert(
        self,
        source_id: str,
        age_s: float,
        threshold_s: float,
        row: dict[str, Any] | None = None,
    ) -> None:
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
        logger.error(
            "liveness_watchdog.source_stall source=%s age_hours=%.1f "
            "threshold_hours=%.1f — emitting alert",
            source_id, age_h, thr_h,
        )
        await self._publish_alert(envelope)


def _source_stall_diagnosis(row: dict[str, Any]) -> str:
    """Turn the source's newest poll-outcome row (DQ-H5b #88) into a WHY
    sentence for the cadence alert body.

    ``row`` carries ``last_poll_outcome`` ('empty'|'error'|None),
    ``last_poll_health`` ('healthy'|'degraded'|'unhealthy'|None),
    ``last_poll_error`` and ``last_poll_at``. A missing/NULL outcome means NO
    non-productive poll was ever recorded — i.e. the poll reminder itself is
    likely not firing (the actor never ran), which is a different failure than
    a feed that runs but produces nothing.
    """
    outcome = row.get("last_poll_outcome")
    when = row.get("last_poll_at")
    when_s = ""
    if isinstance(when, datetime):
        when_s = f" at {when.astimezone(timezone.utc).isoformat(timespec='seconds')}"
    if outcome == "error":
        health = row.get("last_poll_health") or "unknown"
        err = row.get("last_poll_error")
        err_s = f" — {err}" if err else ""
        return (
            f"Last poll FAILED ({health}){when_s}{err_s}. The fetch/parse path "
            "errored (4xx / parse-fail / timeout); fix the source, not the cron."
        )
    if outcome == "empty":
        return (
            f"Last poll fetched cleanly but produced 0 signals (healthy "
            f"HTTP-200, no new items){when_s}. The feed may simply be quiet — "
            "verify upstream actually has nothing, or whether the cursor/filter "
            "is over-trimming."
        )
    # No poll-outcome row at all → the poll likely never ran this window.
    return (
        "No empty/error poll-outcome row was recorded — the poll reminder "
        "itself may be dead (the source actor isn't firing). Check the poll "
        "reminder liveness / actor activation before the fetch path."
    )


def _evaluate_cadence_staleness(
    rows: Any,
    *,
    now: datetime,
    factor: float,
    min_threshold_s: float,
    id_key: str = "analyst_id",
    ts_key: str = "last_run",
) -> list[tuple[str, float, float]]:
    """Pure staleness decision over cadence rows — no DB, unit-testable.

    ``rows``: iterable of mappings with an id (``id_key``), ``cron`` (5-field
    cron string), and a last-activity timestamp (``ts_key``, tz-aware datetime
    or None). Returns ``(id, age_s, threshold_s)`` for each entity whose age
    exceeds ``max(factor × cron_interval, min_threshold_s)``. Rows with no prior
    activity (timestamp None) are skipped — a never-run analyst / never-produced
    source is a registration/activation failure (covered by the boot log), not
    a cadence stall, and skipping it avoids false-alarming fresh registrations.
    Generic over ``id_key``/``ts_key`` so it serves both per-analyst (run) and
    per-source (signal) liveness.
    """
    stale: list[tuple[str, float, float]] = []
    for r in rows:
        analyst_id = r.get(id_key)
        cron = (r.get("cron") or "").strip()
        last_run = r.get(ts_key)
        if not analyst_id or not cron or last_run is None:
            continue
        try:
            _due, period = cron_to_reminder_timing(cron, base_time=now)
        except ValueError:  # invalid cron — skip, don't crash the sweep
            continue
        interval_s = period.total_seconds()
        if interval_s <= 0:
            continue
        threshold_s = max(factor * interval_s, min_threshold_s)
        age_s = (now - last_run).total_seconds()
        if age_s > threshold_s:
            stale.append((analyst_id, age_s, threshold_s))
    return stale


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
