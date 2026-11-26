# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a SUBSCRIBED source's received events become.

:class:`StreamCore` is a :class:`~legba.runtime.source_actor.SourceCore`, so a
streamed record goes through EXACTLY the code a polled one does: the keyword
gate, the baseline enrichment with the transient-skip hold, the document sink
(write, S-4 collapse, ingest-dedupe alias, the NATS publish) or a typed
record's family sink, the descriptor's licence/access stamps. What differs is
only how records arrive (a batch of events off the connector's queue instead
of a handler's ``pull``) and what the cursor is (the last processed event id,
the transport's resume point, instead of a feed timestamp).

RECEIPTS — the same proof chain as a poll. Each received event's bytes, as
they arrived on the wire (``StreamEvent.raw``), become a fetch VERSION
(``fetch_versions``, migration 0245: hashed, stored zstd in the
content-addressed evidence store, deduplicated by hash) and every record
parsed from the event is linked to it (``fetch_version_records``, role
``response``). The ADDRESS of a streamed event is
``<canonical stream url>#event=<event id>`` (the event id percent-encoded);
an event the upstream sent without an id is kept at the canonical stream URL
itself. So a re-delivered event (a resume replay) is the SAME version
(``fetch_count`` moves, nothing is stored), and an upstream that re-sends an
id with different bytes makes a second version of that address — exactly the
version semantics of a re-fetched page. A malformed event is kept too: what
was received is evidence whether or not it parsed.

CURSOR — persisted (``actor_filter_state``, the source's ``cursor`` row) AFTER
the batch's writes, never before: ``last_event_id`` is the id of the last
event whose records were all written or judged. A transient enrichment skip
holds it before the skipped event (which is retried in the next batch, under
the poll path's bounded hold: written unenriched after
``ENRICH_HOLD_MAX_SKIPS``), so a crash resumes from before anything not yet
landed and the content-hash collapse absorbs the overlap.

ONE ROW PER WINDOW — :meth:`StreamCore.write_window` writes the
``source_poll_outcomes`` row a poll writes (same outcome vocabulary, same
health states, ``newest_entry_ts`` for the honest-quiet discriminator) plus
the ``stream`` column (migration 0256). A declared gap is a
``source_stream_gaps`` row (:meth:`StreamCore.write_gap`).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from ..data._url_canon import canonical_url
from ..data.claim_hygiene import poll_claim_hygiene
from ..data.sources._contract import Signal
from ..data.sources.fetch_capture import MAX_CAPTURE_BYTES, FetchCapture
from ..data.sources.stream_event import MalformedStreamEvent, StreamEvent
from ..data.tags import tag_vocabulary_index
from .fetch_receipts import ReceiptRecorder, RecordLink
from .source_actor import SourceCore, SourceDeps
from .source_cursor import raw_feed_ts
from .source_enrich_hold import EnrichHold
from .source_first_poll_age import FirstPollAgeCap
from .source_poll_outcome import classify_poll_outcome, write_poll_outcome
from .source_poll_records import finish_poll, route_record

logger = logging.getLogger("legba.runtime.stream")

__all__ = [
    "GAP_KINDS",
    "StreamCore",
    "StreamGap",
    "SubscribeKindError",
    "WindowStats",
    "receipt_address",
    "window_health",
]

GAP_KINDS = ("overflow", "no_resume", "shutdown")

#: window state -> the ``health_state`` the poll ledger's readers know.
_HEALTH = {
    "connected": "healthy", "quiet": "healthy", "stopped": "healthy",
    "reconnecting": "degraded", "connecting": "degraded",
    "stalled": "unhealthy", "refused": "unhealthy",
}

_INSERT_GAP = """
INSERT INTO public.source_stream_gaps (
    source_id, source_version, owner_tenant, transport, gap_kind, opened_at, closed_at,
    after_event_id, first_dropped_id, last_dropped_id, dropped, detail
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
"""


def _dedupe_events(events: list[StreamEvent]) -> list[StreamEvent]:
    seen: set[int] = set()
    out = []
    for ev in events:
        if id(ev) not in seen:
            seen.add(id(ev))
            out.append(ev)
    return out


class SubscribeKindError(TypeError):
    """The descriptor's kind cannot parse received events (no ``parse_event``)."""


def receipt_address(stream_url: str, event_id: str | None) -> str:
    """The fetch-receipt address of one streamed event (module docstring)."""
    base = canonical_url(stream_url) or stream_url
    return f"{base}#event={quote(event_id, safe='')}" if event_id else base


def window_health(state: str) -> str | None:
    return _HEALTH.get(state)


@dataclass
class StreamGap:
    kind: str
    opened_at: datetime
    closed_at: datetime | None = None
    after_event_id: str | None = None
    first_dropped_id: str | None = None
    last_dropped_id: str | None = None
    dropped: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class WindowStats:
    """What one outcome window of a held connection did."""

    opened_at: datetime
    state: str = "connecting"
    events: int = 0
    records: int = 0
    written: int = 0
    reserve_unchanged: int = 0
    sink_counts: dict[str, Any] = field(default_factory=dict)
    claim_hygiene: dict[str, int] = field(default_factory=dict)  # C-2
    malformed: int = 0
    other_types: int = 0
    age_skipped: int = 0
    enrich_skipped: int = 0
    enrich_bypassed: int = 0
    keyword_filtered: int | None = None
    heartbeats: int = 0
    reconnects: int = 0
    stalls: int = 0
    dropped: int = 0
    gaps: int = 0
    resumed: bool | None = None
    last_error: str | None = None
    newest_entry_ts: datetime | None = None
    last_event_id: str | None = None

    def add_sink_counts(self, counts: dict[str, Any]) -> None:
        for family, c in (counts or {}).items():
            if not isinstance(c, dict):
                continue
            mine = self.sink_counts.setdefault(family, {})
            for k, v in c.items():
                if isinstance(v, int):
                    mine[k] = int(mine.get(k, 0)) + v

    def stream_column(self, transport: str, window_s: float) -> dict[str, Any]:
        return {
            "state": self.state, "transport": transport, "window_s": round(window_s, 1),
            "events": self.events, "records": self.records, "malformed": self.malformed,
            "other_types": self.other_types, "heartbeats": self.heartbeats,
            "reconnects": self.reconnects, "stalls": self.stalls, "dropped": self.dropped,
            "gaps": self.gaps, "resumed": self.resumed, "last_event_id": self.last_event_id,
            "enrichment_skipped": self.enrich_skipped, "enrichment_bypassed": self.enrich_bypassed,
        }


class StreamCore(SourceCore):
    """A :class:`SourceCore` fed by a held connection instead of a poll."""

    def __init__(self, actor_id: str, sd: SourceDeps) -> None:
        super().__init__(actor_id, sd)
        sub = self.descriptor.subscribe
        if self.descriptor.acquisition != "subscribe" or sub is None:
            raise SubscribeKindError(f"{self.descriptor.identity.id} is not a subscribed source")
        self.subscribe = sub
        self.stream_url: str = sub.url
        self.transport: str = sub.transport
        self._parser: Any = None
        self.cursor: dict[str, Any] = {}

    # -- the parser --------------------------------------------------------

    def parser(self) -> Any:
        if self._parser is None:
            handler = self._build_handler()
            if not callable(getattr(handler, "parse_event", None)):
                raise SubscribeKindError(
                    f"kind {self.descriptor.identity.kind!r} cannot serve acquisition='subscribe' "
                    "(it has no parse_event); built subscribe kinds: stream_json"
                )
            self._ingest_gate.observe_handler(handler)
            self._parser = handler
        return self._parser

    def age_cap(self, *, resumed: bool, connected_at: datetime) -> FirstPollAgeCap:
        """The first-poll age cap for one connection: active on any connect
        with no resume point (a first connect, or a stream that cannot resume
        — the upstream may replay its history), for the WHOLE connection."""
        return FirstPollAgeCap(self.parser(), first_poll=not resumed, now=connected_at)

    # -- cursor ------------------------------------------------------------

    async def load_cursor(self) -> dict[str, Any]:
        raw = await self._state_store().get("cursor")
        self.cursor = dict(raw) if isinstance(raw, dict) else {}
        return self.cursor

    @property
    def resume_id(self) -> str | None:
        rid = self.cursor.get("last_event_id")
        return rid if isinstance(rid, str) and rid else None

    @property
    def held_at(self) -> datetime | None:
        """The last moment a session is known to have held the stream (the
        start of a ``no_resume`` gap after a restart)."""
        for key in ("held_at", "last_pulled_at"):
            raw = self.cursor.get(key)
            if isinstance(raw, str):
                try:
                    ts = datetime.fromisoformat(raw)
                except ValueError:
                    continue
                return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return None

    async def note_held(self, at: datetime) -> None:
        """Record that the stream was held up to ``at`` (once per window)."""
        new = {**self.cursor, "held_at": at.isoformat()}
        try:
            await self._state_store().set("cursor", new)
            self.cursor = new
        except Exception:
            logger.warning("stream.cursor.held_failed source=%s", self.descriptor.identity.id)

    async def _persist_cursor(self, *, last_event_id: str | None, written: int,
                              hold: EnrichHold | None, errored: str | None) -> None:
        prior = self.cursor
        now = datetime.now(tz=timezone.utc)
        new = {
            **prior,
            "source_id": self.descriptor.identity.id,
            "acquisition": "subscribe",
            "transport": self.transport,
            "last_pulled_at": now.isoformat(),
            "rows_pulled": int(prior.get("rows_pulled") or 0) + written,
            "last_error": errored,
            **self._ingest_gate.cursor_fields(prior),
        }
        if last_event_id is not None:
            new["last_event_id"] = last_event_id
            new["last_event_at"] = now.isoformat()
        if hold is not None:
            new.update(hold.cursor_fields(errored=errored is not None))
        try:
            await self._state_store().set("cursor", new)
            self.cursor = new
        except Exception:  # the next batch persists again; never mask the writes
            logger.warning("stream.cursor.persist_failed source=%s", self.descriptor.identity.id,
                           exc_info=True)

    # -- one batch ---------------------------------------------------------

    async def ingest_batch(
        self, events: list[StreamEvent], *, stats: WindowStats, age_cap: FirstPollAgeCap,
    ) -> list[StreamEvent]:
        """Land ``events`` (in arrival order). Returns the events to retry in
        the next batch (a transient enrichment skip). Never raises for a bad
        event: a malformed one is counted and kept as a receipt; a write
        failure is recorded on the window and the cursor holds."""
        if not events:
            return []
        self._families = await tag_vocabulary_index()
        ctx = self._make_context()
        parser = self.parser()
        wanted = set(self.subscribe.event_types)
        hold = EnrichHold(self.cursor)
        receipts = ReceiptRecorder(source_id=self.descriptor.identity.id)
        dedup_stats: dict[str, int] = {}
        retry: list[StreamEvent] = []
        cursor_id: str | None = None
        held = False
        written = 0
        errored: str | None = None
        age_before = age_cap.skipped
        pos = 0
        try:
            async with self.deps.pg_pool.acquire() as conn:
                for index, ev in enumerate(events):
                    pos = index
                    idx = self._capture(receipts, ev)
                    if wanted and ev.event not in wanted:
                        stats.other_types += 1
                    else:
                        try:
                            records = parser.parse_event(ctx, ev, stream_url=self.stream_url,
                                                         transport=self.transport)
                        except MalformedStreamEvent as exc:
                            stats.malformed += 1
                            stats.last_error = f"malformed event {ev.id or '-'}: {exc}"[:300]
                            records = []
                        link = RecordLink(idx, locator={"whole": True})   # B-1: the event IS the item
                        skipped = await self._land(conn, ctx, records, link, receipts,
                                                   hold=hold, index=index, stats=stats,
                                                   dedup_stats=dedup_stats, age_cap=age_cap)
                        written += skipped[1]
                        if skipped[0]:
                            retry.append(ev)
                            held = True
                    if not held and ev.id:
                        cursor_id = ev.id
        except Exception as exc:  # a DB failure mid-batch: the cursor holds
            errored = f"{type(exc).__name__}: {exc}"[:500]
            stats.last_error = errored
            logger.warning("stream.batch.error source=%s err=%s", self.descriptor.identity.id, exc)
            # Everything from the failing event on is retried (in memory; the
            # held cursor also replays it on a resume); the content-hash
            # collapse absorbs whatever of it had already landed.
            retry = _dedupe_events(retry + events[pos:])
            cursor_id = None
        sink_counts = await finish_poll(self, receipts)
        stats.add_sink_counts(sink_counts)
        stats.written += written
        stats.reserve_unchanged += int(dedup_stats.get("reserve_unchanged") or 0)
        for k, v in (poll_claim_hygiene(dedup_stats) or {}).items():  # C-2
            stats.claim_hygiene[k] = stats.claim_hygiene.get(k, 0) + v
        stats.enrich_skipped += hold.skipped
        stats.enrich_bypassed += hold.bypassed
        stats.age_skipped += age_cap.skipped - age_before
        await self._persist_cursor(last_event_id=cursor_id, written=written, hold=hold,
                                   errored=errored)
        dropped = self._ingest_gate.last_poll_dropped
        if dropped is not None:
            stats.keyword_filtered = (stats.keyword_filtered or 0) + dropped
        if cursor_id is not None:
            stats.last_event_id = cursor_id
        return retry

    def _capture(self, receipts: ReceiptRecorder, ev: StreamEvent) -> int | None:
        log = receipts.log
        if log is None:
            return None
        body = ev.raw if len(ev.raw) <= MAX_CAPTURE_BYTES else None
        log.add(FetchCapture(
            url=self.stream_url, method="GET", status=200, content_type="text/event-stream",
            content_encoding=None, role="response", fetched_at=ev.received_at, complete=True,
            body=body, sha256=ev.sha256, size=len(ev.raw),
            hash_basis="entity" if body is not None else "wire",
            address=receipt_address(self.stream_url, ev.id),
        ))
        return len(log.captures) - 1

    async def _land(
        self, conn: Any, ctx: Any, records: list[Any], link: RecordLink, receipts: ReceiptRecorder,
        *, hold: EnrichHold, index: int, stats: WindowStats, dedup_stats: dict[str, int],
        age_cap: FirstPollAgeCap,
    ) -> tuple[bool, int]:
        """Land one event's records. Returns (transiently skipped, written)."""
        skipped_any = False
        written = 0
        for rec in records:
            stats.records += 1
            if not isinstance(rec, Signal):   # a typed record of another family
                route_record(self, rec, link, receipts)
                continue
            if age_cap.too_old(rec):          # first connect: counted, not written
                continue
            ts = raw_feed_ts(rec)
            if ts is not None and (stats.newest_entry_ts is None or ts > stats.newest_entry_ts):
                stats.newest_entry_ts = ts
            hold.at(rec, ts or rec.fetched_at, index)
            before = hold.skipped
            sig = await self._process_one(conn, ctx, rec, dedup_stats=dedup_stats, hold=hold)
            if hold.skipped > before:
                skipped_any = True
                continue
            if sig is not None:
                written += 1
                await self._publish([sig])
                await receipts.link(conn, "signal", sig.signal_id, link)
        return skipped_any, written

    # -- the window row and the gap rows -------------------------------------

    async def write_window(self, stats: WindowStats, *, closed_at: datetime) -> str:
        """The poll-outcome-equivalent row for one window. Returns the outcome."""
        health = window_health(stats.state)
        sink_w = sum(int(c.get("written", 0)) for c in stats.sink_counts.values())
        sink_u = sum(int(c.get("unchanged", 0)) for c in stats.sink_counts.values())
        errored = None
        if stats.state in ("reconnecting", "stalled", "refused", "connecting"):
            errored = f"stream_{stats.state}: {stats.last_error or 'no connection this window'}"
        outcome = classify_poll_outcome(
            signals_written=stats.written + sink_w, reserve_unchanged=stats.reserve_unchanged + sink_u,
            errored=errored, health_state=health, keyword_filtered=stats.keyword_filtered,
        )
        window_s = max(0.0, (closed_at - stats.opened_at).total_seconds())
        try:
            async with self.deps.pg_pool.acquire() as conn:
                await write_poll_outcome(
                    conn,
                    source_id=self.descriptor.identity.id,
                    source_version=self.descriptor.identity.version,
                    owner_tenant=self.descriptor.scope.owner_tenant,
                    outcome=outcome,
                    health_state=health,
                    capped=False,
                    signals_written=stats.written,
                    error=errored or (stats.last_error if stats.malformed else None),
                    newest_entry_ts=stats.newest_entry_ts,
                    reserve_unchanged=stats.reserve_unchanged,
                    keyword_filtered=stats.keyword_filtered,
                    sink_counts=stats.sink_counts or None,
                    age_skipped=stats.age_skipped or None,
                    stream=stats.stream_column(self.transport, window_s),
                    claim_hygiene=stats.claim_hygiene or None,
                )
        except Exception:  # provenance must never take the connection down
            logger.warning("stream.window.persist_failed source=%s", self.descriptor.identity.id,
                           exc_info=True)
        return outcome

    async def write_gap(self, gap: StreamGap) -> None:
        try:
            async with self.deps.pg_pool.acquire() as conn:
                await conn.execute(
                    _INSERT_GAP, self.descriptor.identity.id, self.descriptor.identity.version,
                    self.descriptor.scope.owner_tenant or "default", self.transport, gap.kind,
                    gap.opened_at, gap.closed_at, gap.after_event_id, gap.first_dropped_id,
                    gap.last_dropped_id, gap.dropped, json.dumps(gap.detail or {}, default=str),
                )
        except Exception:
            # The gap is still on the window row (stats.gaps) and in the log.
            logger.warning("stream.gap.persist_failed source=%s kind=%s dropped=%s",
                           self.descriptor.identity.id, gap.kind, gap.dropped, exc_info=True)
