# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SUBSCRIBE connector: one held connection per subscribed source (I-4).

WHO OWNS THE CONNECTION. Not the SourceActor: actors are woken by reminders
and deactivated by the sidecar's idle rules, and a turn may not be held open
for days. The connection belongs to a supervised task in the runtime host
(:mod:`.stream_supervisor` starts one :class:`SubscribeConnector` per ACTIVE
subscribed head), so actor deactivation is irrelevant to it. The actor keeps
what it keeps for a push source: the lifecycle record (activate / pause /
retire) the supervisor follows through the registry heads.

SURVIVING RESTARTS. Exactly one runtime replica holds a source's stream: the
connector takes a Postgres session advisory lock keyed by the source id
(:class:`PgAdvisoryLease`) on a connection it keeps for the session; a dead
replica's lock frees with its connection and another replica's supervisor
takes the stream on its next pass. The resume point is the source's cursor
(``actor_filter_state``), persisted after each batch's writes, so a restart
— of the connection, the process or the host — reconnects with the last
processed event id (``Last-Event-ID`` for SSE) and the upstream replays from
there. A connect that CANNOT resume (``resume: none``, events without ids, a
transport without resume) after an earlier session is a DECLARED gap
(``no_resume``, from the last moment the stream was held to the reconnect).

RECONNECT: full-jitter exponential backoff (:func:`.stream_transport.
backoff_delay`), floored by the upstream's own hint (SSE ``retry:``,
``Retry-After``); a permanent refusal (a 4xx, a blocked host, a missing
credential, a licence the ledger marks restrictive) waits the maximum and
reports ``refused``.

BOUNDED MEMORY: the receive queue holds at most ``subscribe.queue_max``
events. A full queue DROPS the arriving event and opens an ``overflow`` gap
that counts every drop until the queue accepts again — written to
``source_stream_gaps`` and counted on the window row. One event is bounded by
``max_event_bytes`` in the transport.

RATE BOUND: a token bucket at ``max_events_per_second`` between the queue and
the sinks; faster arrival waits in the queue (and overflows it, declared).

HEALTH: the window row's state — ``connected`` (events arrived), ``quiet``
(connected, keep-alives only: honest quiet), ``reconnecting``, ``stalled`` (no
byte for ``idle_timeout_seconds``; the connection is dropped and re-opened),
``refused``, ``stopped``. The poll ledger's health states and outcomes follow
(:func:`.stream_ingest.window_health`), so the liveness watchdog, the health
reads and the firing views see a subscribed source as they see a polled one;
:mod:`.stream_liveness` adds the one check a poll does not need (a stream with
no window rows at all, or down for several windows).

SHUTDOWN: the reader closes; what is queued is landed for up to
``drain_seconds``; anything still queued is either replayed on resume (the
cursor holds before it) or, on a stream that cannot resume, declared as a
``shutdown`` gap. The open window is written, the lease released.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

from ..data.schemas.source_subscribe import FORBID_SUBSCRIBE_LICENSE_CLASSES
from ..data.sources.stream_event import StreamEvent
from .stream_ingest import StreamCore, StreamGap, WindowStats
from .stream_transport import (
    Activity,
    StreamStalled,
    TransportError,
    backoff_delay,
    build_transport,
)

logger = logging.getLogger("legba.runtime.stream")

__all__ = ["PgAdvisoryLease", "SubscribeConnector", "TokenBucket"]

_LEDGER_SQL = "SELECT source_host, license_class FROM source_credibility WHERE source_host = ANY($1::text[])"
_LOCK_SQL = "SELECT pg_try_advisory_lock(hashtextextended($1, 0))"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtextextended($1, 0))"
#: A connection that stayed up this long resets the backoff attempt count.
_STABLE_AFTER_S = 30.0
#: Pause after a batch that errored (a DB blip) before retrying it.
_BATCH_ERROR_PAUSE_S = 1.0


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


class PgAdvisoryLease:
    """One replica holds a source's stream: a session advisory lock on a
    connection kept for the session (freed by Postgres if the holder dies)."""

    def __init__(self, pool: Any, source_id: str) -> None:
        self._pool = pool
        self._key = f"legba.subscribe:{source_id}"
        self._cm: Any = None
        self._conn: Any = None

    async def acquire(self) -> bool:
        cm = self._pool.acquire()
        conn = await cm.__aenter__()
        try:
            got = bool(await conn.fetchval(_LOCK_SQL, self._key))
        except Exception:
            await cm.__aexit__(None, None, None)
            raise
        if not got:
            await cm.__aexit__(None, None, None)
            return False
        self._cm, self._conn = cm, conn
        return True

    async def alive(self) -> bool:
        if self._conn is None:
            return False
        try:
            return await self._conn.fetchval("SELECT 1") == 1
        except Exception:
            return False

    async def release(self) -> None:
        cm, conn = self._cm, self._conn
        self._cm = self._conn = None
        if cm is None:
            return
        with contextlib.suppress(Exception):
            await conn.fetchval(_UNLOCK_SQL, self._key)
        with contextlib.suppress(Exception):
            await cm.__aexit__(None, None, None)


class TokenBucket:
    """``rate`` tokens per second, burst ``max(1, rate)``."""

    def __init__(self, rate: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.rate = float(rate)
        self.burst = max(1.0, self.rate)
        self._tokens = self.burst
        self._clock = clock
        self._at = clock()
        self.waited_s = 0.0

    async def take(self) -> None:
        now = self._clock()
        self._tokens = min(self.burst, self._tokens + (now - self._at) * self.rate)
        self._at = now
        if self._tokens < 1.0:
            wait = (1.0 - self._tokens) / self.rate
            self.waited_s += wait
            await asyncio.sleep(wait)
            self._tokens = 0.0
            self._at = self._clock()
        else:
            self._tokens -= 1.0


class SubscribeConnector:
    """Holds one subscribed source's connection and feeds its spine."""

    def __init__(
        self, core: StreamCore, *, lease: Any, client_factory: Any = None,
        drain_seconds: float = 10.0, lease_retry_seconds: float | None = None,
    ) -> None:
        self.core = core
        self.sub = core.subscribe
        self.source_id = core.descriptor.identity.id
        self.lease = lease
        self._client_factory = client_factory
        self._drain_seconds = drain_seconds
        self._lease_retry_s = lease_retry_seconds or max(5.0, self.sub.outcome_window_seconds / 2)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=self.sub.queue_max)
        self.bucket = TokenBucket(self.sub.max_events_per_second)
        self.state = "connecting"
        self._cause: str | None = None
        self.stats = WindowStats(opened_at=_now())
        self._lock = asyncio.Lock()
        self._stopping = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._overflow: StreamGap | None = None
        self._last_received_id: str | None = None
        self._last_enqueued_id: str | None = None
        self._retry_hint_s = 0.0
        self._session_end = asyncio.Event()
        self._inflight: list[tuple[StreamEvent, Any]] = []
        self.windows_written = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "SubscribeConnector":
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self.run(), name=f"legba-subscribe-{self.source_id}")
        return self

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def run(self) -> None:
        try:
            while not self._stopping.is_set():
                try:
                    got = await self.lease.acquire()
                except Exception as exc:
                    logger.warning("stream.lease.error source=%s err=%s", self.source_id, exc)
                    got = False
                if not got:   # another replica holds it (or the DB is down)
                    self.state = "standby"
                    await self._sleep(self._lease_retry_s)
                    continue
                try:
                    await self._session()
                finally:
                    await self.lease.release()
        except asyncio.CancelledError:
            raise
        except Exception:  # never let one source's failure escape the supervisor
            logger.exception("stream.connector.crashed source=%s", self.source_id)

    async def _sleep(self, seconds: float) -> bool:
        """Sleep, waking early on stop. True when stopping."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=max(0.0, seconds))
        return self._stopping.is_set()

    async def _session(self) -> None:
        """One lease-holding session: read, land, write windows — until stop
        or a lost lease (then :meth:`run` tries to take the lease again)."""
        await self.core.load_cursor()
        self.stats = WindowStats(opened_at=_now(), state=self.state)
        # A new session resumes from what LANDED (the persisted cursor), never
        # from what an earlier session merely received.
        self._last_received_id = self._last_enqueued_id = None
        try:
            self.core.parser()   # a kind that cannot parse events refuses loud, before any connect
        except Exception as exc:
            logger.error("stream.refused source=%s err=%s", self.source_id, exc)
            self._set_down("refused", str(exc))
            await self._write_window()
            await self._sleep(self.sub.backoff_max_seconds)
            return
        self._session_end = asyncio.Event()
        reader = asyncio.create_task(self._read_loop(), name=f"legba-subscribe-read-{self.source_id}")
        ingest = asyncio.create_task(self._ingest_loop(), name=f"legba-subscribe-ingest-{self.source_id}")
        window = asyncio.create_task(self._window_loop(), name=f"legba-subscribe-window-{self.source_id}")
        waits = [asyncio.create_task(self._stopping.wait()), asyncio.create_task(self._session_end.wait())]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waits:
                w.cancel()
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
            window.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await window
            was_held = self.state == "connected"
            await self._drain(ingest)
            await self._close_overflow()
            self.state = "stopped"
            await self._write_window(final=True)
            if was_held:
                await self.core.note_held(_now())

    # -- reading -------------------------------------------------------------

    async def _read_loop(self) -> None:
        attempt = 0
        disconnected_at: datetime | None = None
        while not self._stopping.is_set():
            refusal = await self._preflight()
            headers: dict[str, str] = {}
            if refusal is None:
                try:
                    headers = await self._headers()
                except TransportError as exc:
                    refusal = str(exc)
            if refusal is not None:
                self._set_down("refused", refusal)
                if await self._sleep(self.sub.backoff_max_seconds):
                    return
                continue
            try:
                transport = build_transport(self.sub.transport, self.sub, headers=headers,
                                            client_factory=self._client_factory)
            except TransportError as exc:   # an unbuilt transport (refused at save too)
                self._set_down("refused", str(exc))
                if await self._sleep(self.sub.backoff_max_seconds):
                    return
                continue
            # The resume point: within a session, the last event RECEIVED and
            # queued (it is safe in memory); after a restart, the persisted
            # cursor — the last event LANDED.
            resume_id = (self._last_enqueued_id or self.core.resume_id) if (
                self.sub.resume == "last_event_id" and transport.supports_resume) else None
            permanent = False
            retry_after = 0.0
            connected_at: datetime | None = None
            try:
                async with transport.open(resume_id=resume_id) as items:
                    connected_at = _now()
                    await self._on_connected(connected_at, resume_id, disconnected_at)
                    cap = self.core.age_cap(resumed=resume_id is not None, connected_at=connected_at)
                    async for item in items:
                        if isinstance(item, Activity):
                            self.stats.heartbeats += 1 if item.kind == "heartbeat" else 0
                            if item.retry_ms is not None:
                                self._retry_hint_s = item.retry_ms / 1000.0
                            continue
                        await self._enqueue(item, cap)
                self._cause = "upstream closed the stream"
                self._set_down("reconnecting", self._cause)
            except StreamStalled as exc:
                self.stats.stalls += 1
                self._set_down("stalled", str(exc))
            except TransportError as exc:
                permanent = exc.permanent
                retry_after = exc.retry_after or 0.0
                self._set_down("refused" if permanent else "reconnecting", str(exc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # an unexpected parser/transport fault
                logger.warning("stream.read.error source=%s err=%s", self.source_id, exc, exc_info=True)
                self._set_down("reconnecting", f"{type(exc).__name__}: {exc}")
            disconnected_at = _now()
            if connected_at is not None and (disconnected_at - connected_at).total_seconds() >= _STABLE_AFTER_S:
                attempt = 0
            attempt += 1
            delay = self.sub.backoff_max_seconds if permanent else backoff_delay(
                attempt, initial=self.sub.backoff_initial_seconds, cap=self.sub.backoff_max_seconds,
                floor=max(self._retry_hint_s, retry_after))
            logger.info("stream.disconnected source=%s state=%s attempt=%d next_in=%.1fs err=%s",
                        self.source_id, self.state, attempt, delay, self.stats.last_error)
            if await self._sleep(delay):
                return

    async def _on_connected(self, at: datetime, resume_id: str | None,
                            disconnected_at: datetime | None) -> None:
        reconnect = disconnected_at is not None
        if reconnect:
            self.stats.reconnects += 1
        self.state = "connected"
        self.stats.state = "connected"
        self.stats.resumed = resume_id is not None
        logger.info("stream.connected source=%s transport=%s resumed=%s reconnect=%s",
                    self.source_id, self.sub.transport, resume_id is not None, reconnect)
        # No resume point after an earlier session: what the upstream emitted
        # while we were not holding the stream was not received. Declared.
        prior_at = disconnected_at or self.core.held_at
        if resume_id is None and prior_at is not None:
            gap = StreamGap("no_resume", opened_at=min(prior_at, at), closed_at=at,
                            after_event_id=self._last_received_id or self.core.cursor.get("last_event_id"),
                            detail={"reason": self._no_resume_reason()})
            self.stats.gaps += 1
            await self.core.write_gap(gap)

    def _no_resume_reason(self) -> str:
        if self.sub.resume == "none":
            return "descriptor resume=none"
        return "no event id to resume from (the upstream's events carry none)"

    def _set_down(self, state: str, error: str) -> None:
        self.state = state
        self.stats.state = state
        self.stats.last_error = error[:500]

    async def _enqueue(self, ev: StreamEvent, cap: Any) -> None:
        self.stats.events += 1
        self._last_received_id = ev.id or self._last_received_id
        try:
            self.queue.put_nowait((ev, cap))
        except asyncio.QueueFull:
            self.stats.dropped += 1
            gap = self._overflow
            if gap is None:
                self.stats.gaps += 1
                self._overflow = StreamGap(
                    "overflow", opened_at=ev.received_at, after_event_id=self._last_enqueued_id,
                    first_dropped_id=ev.id, last_dropped_id=ev.id, dropped=1,
                    detail={"queue_max": self.sub.queue_max},
                )
                logger.warning("stream.overflow.open source=%s queue_max=%d", self.source_id,
                               self.sub.queue_max)
            else:
                gap.dropped = int(gap.dropped or 0) + 1
                gap.last_dropped_id = ev.id or gap.last_dropped_id
            return
        self._last_enqueued_id = ev.id or self._last_enqueued_id
        if self._overflow is not None:
            await self._close_overflow(at=ev.received_at)

    async def _close_overflow(self, *, at: datetime | None = None) -> None:
        gap, self._overflow = self._overflow, None
        if gap is None:
            return
        gap.closed_at = at or _now()
        logger.warning("stream.overflow.closed source=%s dropped=%s", self.source_id, gap.dropped)
        await self.core.write_gap(gap)

    async def _preflight(self) -> str | None:
        """A refusal reason, or None. The licence ledger is read on EVERY
        connect, so a host the operator marks restrictive is refused at the
        next reconnect."""
        host = (urlsplit(self.sub.url).hostname or "").lower()
        try:
            from ..data.filters.source_credibility import extract_lookup_hosts  # noqa: PLC0415

            cands = extract_lookup_hosts(host) if host else []
            if not cands:
                return None
            async with self.core.deps.pg_pool.acquire() as conn:
                rows = {r["source_host"]: r["license_class"] for r in await conn.fetch(_LEDGER_SQL, cands)}
        except Exception as exc:  # a ledger read failure does not refuse; it is logged
            logger.warning("stream.ledger.read_failed source=%s err=%s", self.source_id, exc)
            return None
        for cand in cands:
            if cand in rows:
                cls = rows[cand]
                if cls in FORBID_SUBSCRIBE_LICENSE_CLASSES:
                    return f"licence ledger marks {cand} {cls!r}: a held connection is refused"
                return None
        return None

    async def _headers(self) -> dict[str, str]:
        headers = dict(self.sub.headers)
        auth = self.sub.auth
        if auth is None:
            return headers
        resolve = getattr(self.core.deps, "secrets_resolve", None)
        if resolve is None:
            raise TransportError(f"subscribe.auth declares {auth.secret_ref!r} but no secrets resolver "
                                 "is wired — refusing to connect unauthenticated", permanent=True)
        try:
            secret = await resolve(auth.secret_ref)
        except Exception as exc:
            raise TransportError(f"subscribe.auth secret {auth.secret_ref!r} did not resolve: "
                                 f"{type(exc).__name__}", permanent=True) from exc
        if isinstance(secret, bytes):
            secret = secret.decode("utf-8")
        if not secret:
            raise TransportError(f"subscribe.auth secret {auth.secret_ref!r} is empty", permanent=True)
        headers[auth.header] = auth.value_template.replace("{secret}", str(secret))
        return headers

    # -- landing -------------------------------------------------------------

    async def _ingest_loop(self) -> None:
        retry: list[tuple[StreamEvent, Any]] = []
        while True:
            batch = retry
            retry = []
            if not batch:
                batch = [await self.queue.get()]
            while len(batch) < self.sub.batch_max:
                try:
                    batch.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            self._inflight = batch
            for _ in batch:
                await self.bucket.take()
            try:
                retry = await self._land(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # never let the landing loop die silently
                logger.warning("stream.land.error source=%s err=%s", self.source_id, exc, exc_info=True)
                self.stats.last_error = f"{type(exc).__name__}: {exc}"[:500]
                retry = batch
            self._inflight = []
            if retry:
                self._inflight = retry
                await asyncio.sleep(_BATCH_ERROR_PAUSE_S)

    async def _land(self, batch: list[tuple[StreamEvent, Any]]) -> list[tuple[StreamEvent, Any]]:
        """Land a batch, grouped by the connection (age cap) each event came on."""
        retry: list[tuple[StreamEvent, Any]] = []
        i = 0
        while i < len(batch):
            cap = batch[i][1]
            j = i
            while j < len(batch) and batch[j][1] is cap:
                j += 1
            events = [ev for ev, _ in batch[i:j]]
            async with self._lock:
                again = await self.core.ingest_batch(events, stats=self.stats, age_cap=cap)
            retry.extend((ev, cap) for ev in again)
            i = j
        return retry

    async def _drain(self, ingest: asyncio.Task) -> None:
        """Land what is queued for up to ``drain_seconds``, then stop the
        ingest loop; what is still queued is replayed on resume or declared."""
        deadline = time.monotonic() + self._drain_seconds
        while ((not self.queue.empty() or self._inflight) and time.monotonic() < deadline
               and not ingest.done()):
            await asyncio.sleep(0.05)
        ingest.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await ingest
        # A batch cut mid-landing is counted as not landed (the content-hash
        # collapse absorbs whatever of it did land, on a replay).
        left: list[StreamEvent] = [ev for ev, _ in self._inflight]
        self._inflight = []
        while True:
            try:
                left.append(self.queue.get_nowait()[0])
            except asyncio.QueueEmpty:
                break
        if not left:
            return
        resumable = self.sub.resume == "last_event_id" and self.core.resume_id is not None and all(
            ev.id for ev in left)
        if resumable:
            logger.info("stream.shutdown.queued source=%s events=%d (replayed on resume from %s)",
                        self.source_id, len(left), self.core.resume_id)
            return
        self.stats.gaps += 1
        self.stats.dropped += len(left)
        await self.core.write_gap(StreamGap(
            "shutdown", opened_at=left[0].received_at, closed_at=_now(),
            after_event_id=self.core.resume_id, first_dropped_id=left[0].id,
            last_dropped_id=left[-1].id, dropped=len(left),
            detail={"reason": "stopped with events queued on a stream that cannot resume"},
        ))

    # -- windows -------------------------------------------------------------

    async def _window_loop(self) -> None:
        while True:
            await asyncio.sleep(self.sub.outcome_window_seconds)
            if not await self.lease.alive():
                logger.warning("stream.lease.lost source=%s — ending this session", self.source_id)
                self._session_end.set()
                return
            await self._write_window()

    async def _write_window(self, *, final: bool = False) -> None:
        async with self._lock:
            stats = self.stats
            closed = _now()
            if self.state == "connected":
                stats.state = "connected" if stats.events > 0 else "quiet"
            else:
                stats.state = self.state
            # The next window starts in the CURRENT state, carrying the
            # evidence the watchdog's honest-quiet test reads.
            self.stats = WindowStats(opened_at=closed, state=self.state,
                                     newest_entry_ts=stats.newest_entry_ts,
                                     last_event_id=stats.last_event_id or self.core.resume_id)
            outcome = await self.core.write_window(stats, closed_at=closed)
            self.windows_written += 1
            if self.state == "connected":
                await self.core.note_held(closed)
        logger.info("stream.window source=%s state=%s outcome=%s events=%d written=%d dropped=%d "
                    "gaps=%d reconnects=%d stalls=%d%s", self.source_id, stats.state, outcome,
                    stats.events, stats.written, stats.dropped, stats.gaps, stats.reconnects,
                    stats.stalls, " final" if final else "")
