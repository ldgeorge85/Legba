# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill / catch-up for late-joining subscriptions (P-12 / PIVOT §4.4 + §6.1).

A target that subscribes AFTER signals already exist — or a source that is
discovered and auto-wires into a running target — must receive BOTH:

  * a one-time **catch-up** over the persistent signals pool (the matching
    historical slice), and
  * the live **forward** NATS stream, picking up exactly where the catch-up
    left off — no gap, no duplicate at the handoff.

The seam reuses the W2 pieces verbatim: the structured SQL ``WHERE`` + Starlark
residual (:mod:`.filter`, via :meth:`SubscriptionEngine.read_slice`) IS the
backfill query — there is no second matching path and no re-pull from the source
(the persistent pool is authoritative for history). The forward path is the same
per-target durable consumer the engine already binds (:mod:`.engine`).

The cursor contract (the no-gap/no-dup guarantee)
-------------------------------------------------
JetStream assigns every published signal a monotonic stream sequence. We treat
that sequence as the single ordering axis that BOTH the backfill and the forward
stream agree on:

  1. **Capture** the stream's current ``last_seq`` once, at catch-up time
     (:func:`capture_cursor`). Call it ``boundary_seq``. By construction every
     signal already on the bus has ``seq <= boundary_seq``; every signal that
     arrives later gets ``seq > boundary_seq``.
  2. **Backfill** the persistent pool up to that boundary
     (:meth:`Backfiller.backfill`). Because the actor writes each signal to
     Postgres *before* it publishes to NATS (P-06 acquisition order), the rows
     visible in the pool at capture time are a SUPERSET of what the stream has
     numbered ``<= boundary_seq``; bounding the catch-up read by the captured
     wall-clock watermark keeps the two halves from overlapping.
  3. **Forward** from ``boundary_seq + 1`` exactly
     (:meth:`Backfiller.bind_forward`) via ``DeliverPolicy.BY_START_SEQUENCE``
     with ``opt_start_seq = boundary_seq + 1``. The live consumer therefore
     resumes at the very next message after the boundary.

The two halves TILE the sequence space exactly once: backfill ≤ boundary,
forward > boundary. That is the no-gap/no-dup handoff the acceptance criterion
demands. Capturing the boundary BEFORE the backfill read (rather than after) is
deliberate — anything that lands during the read is numbered ``> boundary_seq``
and is delivered by the forward consumer, never dropped.

Without a connected NATS store the backfill still runs (pure persistent-pool
catch-up over ``legba_pivot_test``); ``bind_forward`` is then a no-op and the
caller drives delivery from the pool only. This keeps the module unit-testable
against Postgres alone while the full seam runs end-to-end on the dev rig.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from ...data.nats import SIGNAL_STREAM_NAME
from .engine import SubscriptionEngine, TargetSubscription, target_consumer_name
from .subjects import ResolvedBinding

logger = logging.getLogger(__name__)

# A sink consumes one backfilled signal row. Async so a real sink (NATS replay,
# actor inbox, alert dispatch) can await I/O; the in-test sink just appends.
SignalSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class BackfillCursor:
    """The handoff boundary between catch-up and the live forward stream.

    ``boundary_seq`` is the stream's ``last_seq`` captured at catch-up time:
    history is ``seq <= boundary_seq``, forward is ``seq > boundary_seq``.
    ``captured_at`` is the wall-clock watermark the persistent-pool read is
    bounded by, so the catch-up slice cannot overlap the forward slice even as
    new rows land mid-read. ``stream_present`` is False when there is no NATS
    store (pure pool catch-up) — then the cursor is informational only.
    """

    boundary_seq: int
    captured_at: datetime
    stream_present: bool

    @property
    def forward_start_seq(self) -> int:
        """First stream sequence the forward consumer must deliver."""
        return self.boundary_seq + 1


@dataclass
class BackfillResult:
    """Outcome of one target's catch-up + forward bind."""

    target_id: str
    cursor: BackfillCursor
    delivered: int
    delivered_ids: list[str] = field(default_factory=list)
    forward_consumer: str | None = None


async def capture_cursor(engine: SubscriptionEngine) -> BackfillCursor:
    """Capture the handoff boundary BEFORE reading the persistent pool.

    Reads the shared signal stream's ``last_seq``; everything already published
    is ``<= boundary_seq``. With no NATS store the boundary is 0 (pure pool
    catch-up) and ``stream_present`` is False.
    """
    captured_at = datetime.now(tz=timezone.utc)
    nats = engine._nats  # the engine owns the (optional) connected store
    if nats is None:
        return BackfillCursor(boundary_seq=0, captured_at=captured_at, stream_present=False)
    # Ensure the stream exists so a brand-new deployment has a real (empty)
    # boundary of 0 rather than raising.
    await engine.ensure_signal_stream()
    growth = await nats.stream_growth(SIGNAL_STREAM_NAME)
    return BackfillCursor(
        boundary_seq=int(growth.get("last_seq") or 0),
        captured_at=captured_at,
        stream_present=True,
    )


class Backfiller:
    """Drives the catch-up → forward handoff for late-joining subscriptions.

    Holds the W2 :class:`SubscriptionEngine` (its ``read_slice`` IS the
    predicate-filtered pool query) and the optional connected NATS store (for
    the sequence-anchored forward consumer).
    """

    def __init__(self, engine: SubscriptionEngine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # Catch-up over the persistent pool (predicate-filtered, no re-pull)
    # ------------------------------------------------------------------

    async def _read_binding_history(
        self,
        binding: ResolvedBinding,
        cursor: BackfillCursor,
        *,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        """The historical slice for ONE binding, bounded by the cursor.

        Reuses the engine's ``read_slice`` (W2 structured SQL ``WHERE`` +
        Starlark residual — :mod:`.filter`) and then drops anything fetched
        AFTER the captured watermark. Those later rows belong to the forward
        stream (``seq > boundary_seq``); keeping them here would double-deliver.
        """
        rows = await self._engine.read_slice(binding, limit=limit)
        watermark = cursor.captured_at
        kept: list[dict[str, Any]] = []
        for r in rows:
            fetched = _as_aware(r.get("fetched_at"))
            if fetched is not None and fetched > watermark:
                # Landed after the boundary capture → forward stream's job.
                continue
            kept.append(r)
        return kept

    async def backfill(
        self,
        subscription: TargetSubscription,
        cursor: BackfillCursor,
        sink: SignalSink,
        *,
        limit_per_binding: int | None = None,
    ) -> tuple[int, list[str]]:
        """Deliver the matching historical slice once, oldest-first.

        Unions the per-binding history (deduped on signal id — a row that
        matches via two bindings is delivered once) and replays it through
        ``sink`` in chronological (``fetched_at`` ascending) order, so a target
        sees catch-up in the same order it will see the forward stream. No
        re-pull: every row comes from the persistent pool. Returns
        ``(count, delivered_ids)``.
        """
        seen: set[str] = set()
        merged: list[dict[str, Any]] = []
        for b in subscription.bindings:
            for row in await self._read_binding_history(
                b, cursor, limit=limit_per_binding
            ):
                rid = str(row.get("id"))
                if rid in seen:
                    continue
                seen.add(rid)
                merged.append(row)

        # Chronological replay (the live forward stream is monotonic in time).
        merged.sort(key=lambda r: (_sort_key(r.get("fetched_at")), str(r.get("id"))))

        delivered_ids: list[str] = []
        for row in merged:
            await sink(row)
            delivered_ids.append(str(row.get("id")))
        return len(delivered_ids), delivered_ids

    # ------------------------------------------------------------------
    # Forward bind — resume the live stream exactly after the boundary
    # ------------------------------------------------------------------

    async def bind_forward(
        self,
        subscription: TargetSubscription,
        cursor: BackfillCursor,
    ) -> str | None:
        """(Re)bind the per-target durable consumer to start at boundary+1.

        Uses ``DeliverPolicy.BY_START_SEQUENCE`` anchored on
        ``cursor.forward_start_seq`` so the forward stream delivers the FIRST
        message after the captured boundary and nothing before it. This is the
        live half of the no-gap/no-dup handoff. No-op (returns None) without a
        connected NATS store or with no authorized bindings.
        """
        nats = self._engine._nats
        if nats is None or not subscription.bindings:
            return None

        await self._engine.ensure_signal_stream()
        consumer = subscription.consumer_name or target_consumer_name(
            subscription.target_id
        )
        await _ensure_consumer_from_seq(
            nats,
            stream=SIGNAL_STREAM_NAME,
            durable=consumer,
            filter_subjects=subscription.subject_filters,
            start_seq=cursor.forward_start_seq,
        )
        return consumer

    # ------------------------------------------------------------------
    # The full seam — capture → backfill → forward
    # ------------------------------------------------------------------

    async def catch_up_and_forward(
        self,
        subscription: TargetSubscription,
        sink: SignalSink,
        *,
        limit_per_binding: int | None = None,
        cursor: BackfillCursor | None = None,
    ) -> BackfillResult:
        """Run the complete late-join handoff for one registered target.

        Order matters: capture the boundary FIRST (so anything that lands
        during the read is forward, never lost), backfill the pool up to it,
        THEN bind the forward consumer at boundary+1. Pass a pre-captured
        ``cursor`` to anchor the boundary at registration time (e.g. the engine
        captures it inside ``register_target`` before any read).
        """
        if cursor is None:
            cursor = await capture_cursor(self._engine)

        delivered, delivered_ids = await self.backfill(
            subscription, cursor, sink, limit_per_binding=limit_per_binding
        )
        forward_consumer = await self.bind_forward(subscription, cursor)

        logger.info(
            "backfill target=%s boundary_seq=%d delivered=%d forward_from=%d consumer=%s",
            subscription.target_id,
            cursor.boundary_seq,
            delivered,
            cursor.forward_start_seq,
            forward_consumer,
        )
        return BackfillResult(
            target_id=subscription.target_id,
            cursor=cursor,
            delivered=delivered,
            delivered_ids=delivered_ids,
            forward_consumer=forward_consumer,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _ensure_consumer_from_seq(
    nats: Any,
    *,
    stream: str,
    durable: str,
    filter_subjects: list[str],
    start_seq: int,
) -> Any:
    """Idempotently (re)create a durable consumer that starts at ``start_seq``.

    JetStream cannot edit a durable's deliver policy / start sequence in place,
    so if a consumer already exists with a different anchor we delete + recreate
    it. Mirrors ``NatsStore.ensure_durable_consumer`` but pins
    ``DeliverPolicy.BY_START_SEQUENCE`` — kept here (not in ``data/nats.py``) so
    the sequence-anchored forward bind lives entirely in the backfill seam.
    """
    from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
    from ...data.nats import SIGNAL_SUBJECT_ROOT

    wanted = sorted(set(filter_subjects)) or [f"{SIGNAL_SUBJECT_ROOT}.>"]
    cfg_kwargs: dict[str, Any] = {
        "durable_name": durable,
        "deliver_policy": DeliverPolicy.BY_START_SEQUENCE,
        "opt_start_seq": max(int(start_seq), 1),
        "ack_policy": AckPolicy.EXPLICIT,
        "max_ack_pending": 1000,
    }
    if len(wanted) == 1:
        cfg_kwargs["filter_subject"] = wanted[0]
    else:
        cfg_kwargs["filter_subjects"] = wanted

    js = nats.js
    try:
        existing = await js.consumer_info(stream, durable)
    except Exception:
        existing = None

    if existing is not None:
        # Always recreate: the forward bind redefines the start anchor, and a
        # durable created earlier (e.g. DeliverPolicy.ALL at registration) would
        # otherwise re-deliver history the backfill already covered.
        try:
            await js.delete_consumer(stream, durable)
        except Exception as exc:  # pragma: no cover - best-effort cleanup
            logger.warning("delete_consumer %s/%s failed: %s", stream, durable, exc)

    await js.add_consumer(stream, ConsumerConfig(**cfg_kwargs))
    return await js.consumer_info(stream, durable)


def _as_aware(value: Any) -> datetime | None:
    """Coerce a row ``fetched_at`` to a tz-aware UTC datetime (or None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return None


def _sort_key(value: Any) -> datetime:
    """Sort key for chronological replay; missing timestamps sort earliest."""
    dt = _as_aware(value)
    return dt if dt is not None else datetime.min.replace(tzinfo=timezone.utc)


__all__ = [
    "BackfillCursor",
    "BackfillResult",
    "Backfiller",
    "SignalSink",
    "capture_cursor",
]
