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

B0-12 (durable per-entity alerts + precision): the per-analyst / per-source
cadence checks and the per-source empty-streak check alert on the
STATE-TRANSITION EDGE — one durable ``alert_sink_deliveries`` row
(``sink_kind='liveness_watchdog'``, ``payload_summary.state``
'entered'/'recovered') plus a P1-1 outward fan-out per transition, silence
while a condition persists, restart-safe via a ledger-seeded state map. The
empty-streak check additionally discriminates honest-quiet feeds (newest
observed upstream entry <= our last ingest → NO alert) from cursor/filter
faults (upstream carries newer entries yet polls yield 0 → escalate) using
the ``newest_entry_ts`` evidence the source handlers record per poll.
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
# How many recent poll-outcome rows per source the streak read pulls back. The
# streak only counts leading 'empty' rows, so a window comfortably above the
# threshold is enough to confirm the run and see the breaking row (if any).
_EMPTY_STREAK_WINDOW = 20

# B0-12 — durable per-entity alert channels (``alert_sink_deliveries.
# channel_name``; ``sink_target`` carries the entity id). The per-analyst /
# per-source checks used to only publish to the streamless NATS subject —
# alerts EVAPORATED (no durable consumer) and a persistently-bad entity was
# re-escalated on a heartbeat (~1.2k ERROR lines/day across a dozen degraded
# sources, 2026-07-21 review). Now each check alerts on the STATE-TRANSITION
# EDGE, not the level: one durable ``state='entered'`` row (+ P1-1 outward
# fan-out) when an entity enters the bad state, one ``state='recovered'`` row
# when it leaves it, silence in between. The last-alerted state is re-seeded
# from the ledger on boot so a restart cannot re-fire an ongoing condition.
ALERT_CHANNEL_GLOBAL = "liveness_stall"
ALERT_CHANNEL_ANALYST = "analyst_cadence_stall"
ALERT_CHANNEL_SOURCE = "source_cadence_stall"
ALERT_CHANNEL_SOURCE_DEGRADED = "source_degraded"
_TRANSITION_CHANNELS = (
    ALERT_CHANNEL_ANALYST,
    ALERT_CHANNEL_SOURCE,
    ALERT_CHANNEL_SOURCE_DEGRADED,
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

    @classmethod
    def from_env(cls) -> "WatchdogConfig":
        return cls(
            stall_after_s=_env_float(_STALL_AFTER_ENV, _DEFAULT_STALL_MINUTES) * 60.0,
            realert_every_s=_env_float(_REALERT_ENV, _DEFAULT_REALERT_MINUTES) * 60.0,
            check_interval_s=_env_float(_INTERVAL_ENV, _DEFAULT_CHECK_SECONDS),
            cadence_stall_factor=_env_float(_CADENCE_FACTOR_ENV, _DEFAULT_CADENCE_FACTOR),
            empty_streak_threshold=_env_int(_EMPTY_STREAK_ENV, _DEFAULT_EMPTY_STREAK),
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
        # P1-1 — optional outward alert-sink dispatcher
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
        # P1-1: outward fan-out (webhook etc.) — a stall alert that only ever
        # landed internal rows still needed a human to be LOOKING; when a sink
        # is configured this pages the operator endpoint directly.
        await self._fan_out_alert_sinks(title, body)

    async def _fan_out_alert_sinks(
        self,
        title: str,
        body: str,
        *,
        channel_name: str = ALERT_CHANNEL_GLOBAL,
        severity: str = "high",
    ) -> None:
        """Push an alert through the P1-1 alert-sink dispatcher.

        Guarded — the watchdog loop must never die on the outward edge (the
        dispatcher itself never raises; this wraps the payload shaping too).
        """
        if self._alert_sinks is None:
            return
        try:
            from ..data.alerts.sinks import runtime_alert_payload

            payload = runtime_alert_payload(
                channel_name=channel_name,
                summary=title,
                detail=body,
                severity=severity,
            )
            await self._alert_sinks.fan_out(payload)
        except Exception as exc:  # pragma: no cover — never crash the loop
            logger.warning(
                "liveness_watchdog.alert_sink_fanout_failed err=%s", exc
            )

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
    ) -> None:
        """Mark an entity's alert-state transition (entry or recovery).

        In-memory FIRST (the edge gate must hold this process even when the
        durable write fails — else a broken DB turns the edge back into a
        60s-level firehose), then the durable ``alert_sink_deliveries`` row,
        then the P1-1 outward fan-out. Every leg is fail-safe.
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
        await self._fan_out_alert_sinks(
            title, body, channel_name=channel, severity=severity,
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
        streamless NATS publish — the durable row + P1-1 fan-out are the
        operator-visible surfaces.
        """
        states = await self._get_alert_states(channel)
        for entity_id, state in list(states.items()):
            if state != "entered" or entity_id in still_bad:
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
        fires ONCE (streamless NATS + durable ``entered`` row + P1-1 outward
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
            if states.get(source_id) == "entered":
                continue  # ongoing condition — the entry edge already fired
            await self._emit_source_cadence_alert(
                source_id, age_s, threshold_s, rows_by_id.get(source_id) or {},
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
                       po.occurred_at                                   AS last_poll_at,
                       po.newest_entry_ts                               AS last_poll_newest_entry_ts
                FROM source_descriptors d
                LEFT JOIN signals s ON s.source_id = d.descriptor_id
                LEFT JOIN LATERAL (
                    SELECT outcome, health_state, error, occurred_at,
                           newest_entry_ts
                    FROM source_poll_outcomes
                    WHERE source_id = d.descriptor_id
                    ORDER BY occurred_at DESC
                    LIMIT 1
                ) po ON TRUE
                WHERE d.is_head AND d.state = 'active'
                GROUP BY 1, 2, po.outcome, po.health_state, po.error,
                         po.occurred_at, po.newest_entry_ts
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
        # B0-12: durable entry edge + outward fan-out.
        await self._record_transition(
            channel=ALERT_CHANNEL_SOURCE,
            entity_id=source_id,
            state="entered",
            severity="high",
            kind="source_cadence_stall",
            title=title,
            body=body,
            extra={"age_hours": round(age_h, 1)},
        )

    # -- OBS: per-source empty-200 streak escalation (D19) ---------------

    async def check_source_empty_streak_once(self, now_monotonic: float) -> list[str]:
        """Escalate any source whose most-recent CONSECUTIVE 'empty' poll-outcome
        run has reached ``empty_streak_threshold`` — unless the run is
        classified honest-quiet.

        This catches the xinhua/aljazeera dead-15d class: a feed that keeps
        firing HTTP-200-with-0-items, logs every poll health='healthy', and so
        never registers as an error — yet is functionally dead. The cadence
        check eventually trips on signal staleness, but for a 6-hourly source
        that is many hours later; the empty-streak run reaches the threshold
        much sooner.

        B0-12 precision: each empty poll-outcome row may carry the handler's
        ``newest_entry_ts`` observation, which splits the qualifying streaks
        (see :func:`_evaluate_empty_streaks`) into honest-quiet (feed simply
        has nothing newer than our last ingest → NO alert — this was the
        B0-11 false-quiet class), cursor_fault (feed CARRIES newer entries
        yet the polls yield 0 — the cursor/filter is eating live content →
        escalate high), and unknown (no evidence → the pre-existing degraded
        escalation). Leader-gated, boot-graced, transition-edge alerted
        (entry + recovery only; a continuing episode is silent).
        """
        if self._pg is None:
            return []
        if self._is_leader is not None and not self._is_leader():
            return []
        if (now_monotonic - self._started_at) < self._cfg.stall_after_s:
            return []
        rows = await self._fetch_source_empty_streak_rows()
        degraded = _evaluate_empty_streaks(
            rows, threshold=self._cfg.empty_streak_threshold
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

    async def _fetch_source_empty_streak_rows(self) -> list[dict[str, Any]]:
        """The recent poll-outcome run per ACTIVE source, newest-first, plus the
        source's newest produced-signal timestamp.

        Pulls the last ``_EMPTY_STREAK_WINDOW`` ``source_poll_outcomes`` rows for
        each active source. A PRODUCTIVE poll writes NO outcome row, so it cannot
        break the empty run from THIS table by itself — which is why every row
        also carries ``last_signal`` (``max(signals.fetched_at)`` for the source):
        ``_evaluate_empty_streaks`` uses it to RESET a streak whose most-recent
        empty poll is OLDER than a produced signal (the source is alive again).
        Without it, an actively-producing source whose last recorded outcome rows
        happen to be empty stays pinned DEGRADED forever. Each output row is one
        outcome: ``{source_id, outcome, occurred_at, health_state,
        newest_entry_ts, last_signal}`` — ``newest_entry_ts`` (B0-12) is the
        handler's newest-observed-upstream-entry evidence the discriminator
        keys on (NULL for handlers that don't record it).
        """
        # _EMPTY_STREAK_WINDOW is a trusted module-level int constant (not user
        # input); inlined into the LATERAL LIMIT.
        window = int(_EMPTY_STREAK_WINDOW)
        async with self._pg.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT po.source_id        AS source_id,
                       po.outcome          AS outcome,
                       po.occurred_at      AS occurred_at,
                       po.health_state     AS health_state,
                       po.newest_entry_ts  AS newest_entry_ts,
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
            # (the B0-11 stategov cursor-poison class). This is the escalation
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
                "feed (the B0-11 stategov cursor-poison class): check the "
                "stored cursor timestamp (future-skew poison), the since-"
                "filter, and the ingestion/dedupe filters for this source."
            )
            tag = "cursor_fault"
            kind = "source_cursor_fault"
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
        logger.error(
            "liveness_watchdog.source_degraded source=%s empty_streak=%d "
            "threshold=%d fault_class=%s — escalating",
            source_id, streak, self._cfg.empty_streak_threshold, fault_class,
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


def _as_aware_utc(dt: datetime) -> datetime:
    """Normalize a possibly-naive datetime to aware UTC for comparison."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _classify_streak(
    newest_entry_ts: datetime | None, last_signal: datetime | None
) -> str:
    """B0-12 empty-streak precision discriminator — the truth table.

    ``newest_entry_ts``: the newest upstream-entry timestamp the handler
    OBSERVED across the streak's polls (pre-since-filter, future-skew-clamped
    at the handler). ``last_signal``: the source's newest INGESTED signal.

      * no observation             → 'unknown'      (keep the old degraded path)
      * never ingested + observed  → 'cursor_fault' (feed has entries, we
                                                     yielded none — ever)
      * observed >  last ingest    → 'cursor_fault' (upstream published, the
                                                     cursor/filter ate it)
      * observed <= last ingest    → 'honest_quiet' (feed is simply quiet —
                                                     NO alert)
    """
    if newest_entry_ts is None:
        return "unknown"
    if last_signal is None:
        return "cursor_fault"
    if _as_aware_utc(newest_entry_ts) > _as_aware_utc(last_signal):
        return "cursor_fault"
    return "honest_quiet"


def _source_stall_is_honest_quiet(row: dict[str, Any]) -> bool:
    """B0-12 cadence-precision gate: is this cadence-stale source just QUIET?

    True when the source's freshest poll evidence shows a clean empty poll
    whose newest OBSERVED upstream entry is <= the source's newest ingested
    signal — the feed itself has produced nothing new since we last ingested
    (weekly/monthly feeds, slow news day), so a staleness alert would be the
    B0-11 false-positive class. Any error outcome, missing evidence, or a
    never-ingested source keeps the alert.
    """
    if (row.get("last_poll_outcome") or "") != "empty":
        return False
    newest = row.get("last_poll_newest_entry_ts")
    last_signal = row.get("last_signal")
    if not isinstance(newest, datetime) or not isinstance(last_signal, datetime):
        return False
    return _as_aware_utc(newest) <= _as_aware_utc(last_signal)


def _evaluate_empty_streaks(
    rows: Any,
    *,
    threshold: int,
) -> list[tuple[str, int, Any, str]]:
    """Pure empty-streak decision over poll-outcome rows — no DB, unit-testable.

    ``rows``: iterable of mappings with ``source_id``, ``outcome``
    ('empty'|'error'), ``occurred_at`` (tz-aware datetime), and (optionally)
    ``last_signal`` (tz-aware datetime — the source's newest produced signal)
    and ``newest_entry_ts`` (the handler's newest-observed-upstream-entry
    evidence for that poll — B0-12), already grouped per source and ordered
    NEWEST-FIRST (the SQL guarantees this). For each source, count the leading
    run of consecutive 'empty' rows; the run breaks on the first 'error' row.
    Returns ``(source_id, streak_len, newest_empty_at, fault_class)`` for
    every source whose leading empty run is >= ``threshold``, is NOT producing
    again, and is NOT classified honest-quiet.

    B0-12 discriminator: the run's newest non-null ``newest_entry_ts`` is
    compared against ``last_signal`` by :func:`_classify_streak` —
    'honest_quiet' runs (upstream's newest observed entry <= our last ingest:
    the feed is simply quiet) are EXCLUDED (no alert; 7/8 of the B0-11
    "stalled cluster" were this class); 'cursor_fault' runs (upstream carries
    NEWER entries yet 0 yielded) and 'unknown' runs (no evidence — handlers
    that don't record the observation, pre-B0-12 rows) are returned.

    Signal BOUND (supersedes the older newest-empty-only reset): a PRODUCTIVE
    poll writes NO outcome row (it is self-evidencing via its signals), so this
    table alone cannot see successes. The streak therefore counts ONLY the
    leading 'empty' rows that occurred SINCE ``last_signal`` — an empty poll at
    or before the newest produced signal is stale evidence and stops the count.
    Without the bound, a source that produces daily but polls more often than
    it publishes accumulates an unbounded interleaved 'empty' run and is
    false-flagged (the foreignaffairs 2026-07-21 case: 2 signals ingested that
    morning, "empty_streak=20" alarm the same day). When ``last_signal`` is
    absent (older callers / a source that has never produced), the contiguous
    'empty' run alone decides the streak, which is exactly the
    xinhua/aljazeera dead case.
    """
    if threshold <= 0:
        return []
    degraded: list[tuple[str, int, Any, str]] = []
    by_source: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for r in rows:
        sid = r.get("source_id")
        if not sid:
            continue
        if sid not in by_source:
            by_source[sid] = []
            order.append(sid)
        by_source[sid].append(r)
    for sid in order:
        outcomes = by_source[sid]
        # All rows carry the same per-source last_signal; read it off the first.
        last_signal = outcomes[0].get("last_signal") if outcomes else None
        # Rows already newest-first; count the leading 'empty' run, bounded to
        # the rows SINCE the newest produced signal (see docstring — an empty
        # poll at/before the signal is stale evidence, not part of the run).
        streak = 0
        newest_empty_at: Any = None
        newest_observed: datetime | None = None
        for r in outcomes:
            if (r.get("outcome") or "") != "empty":
                break
            occurred = r.get("occurred_at")
            if (
                last_signal is not None
                and occurred is not None
                and occurred <= last_signal
            ):
                break
            if newest_empty_at is None:
                newest_empty_at = occurred
            # B0-12: aggregate the run's upstream-observation evidence.
            observed = r.get("newest_entry_ts")
            if isinstance(observed, datetime):
                observed = _as_aware_utc(observed)
                if newest_observed is None or observed > newest_observed:
                    newest_observed = observed
            streak += 1
        if streak >= threshold:
            fault_class = _classify_streak(newest_observed, last_signal)
            if fault_class == "honest_quiet":
                continue  # the feed is simply quiet — stay silent (B0-12)
            degraded.append((sid, streak, newest_empty_at, fault_class))
    return degraded


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
