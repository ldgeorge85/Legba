# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Inbound-webhook drain — the S1 accept-and-enqueue back half.

The webhook front (:meth:`legba.data.sources.generic_webhook.
GenericWebhookSourceHandler.handle_webhook`) does MINIMAL work: validate + auth
+ publish the RAW payload envelope onto ``legba.inbound.<source_id>`` and return
202. This module is the durable PULL CONSUMER that pulls ``legba.inbound.>`` OFF
the request path and runs the EXISTING ingest → ``write_canonical_signal`` →
``legba.signals.>`` pipeline, so an external caller never blocks on the DB
insert and a burst gets bounded buffering + durability from the stream.

It mirrors :mod:`legba.runtime.nats_informer` almost verbatim — the same durable
pull-consumer + self-heal rebind shape — with three S1-specific differences:

  * **DeliverPolicy.ALL** (not ``NEW``). The drain has NO other catch-up path
    (unlike the informer, whose ``ReconcileLoop`` periodic resync covers gaps),
    so it MUST replay every buffered/un-acked envelope after a restart. A killed
    drain buffers (WORKQUEUE retention on ``legba_inbound``) and catches up on
    restart.
  * **ACK-AFTER-WRITE.** ``msg.ack()`` is called ONLY after
    ``handler.ingest_and_emit`` returns successfully. A crash in the
    write-before-ack window redelivers the envelope; the write path is
    idempotent (deterministic ``signal_id`` + the P-02 content_hash alias
    backstop) so a redelivery never double-writes a canonical signal.
  * **Dead-letter vs redeliver.** An envelope the drain cannot parse (bad
    base64 / not JSON) or whose ``ingest()`` raises ``ValueError`` /
    ``PermissionError`` (unparseable body / token mismatch on re-verify) is
    ``term()``-ed with a structured ``inbound_drain.deadletter`` log — never
    silently acked away. A TRANSIENT failure (handler not yet registered at
    boot, a DB blip) is ``nak()``-ed for redelivery, ``term()``-ed only after
    ``max_deliver`` attempts.

WORKQUEUE constraint: only ONE consumer filter may overlap each subject on a
work-queue stream — correct for this single S1 drain. A future multi-drain
scale-out must switch ``legba_inbound`` to ``interest`` retention + per-instance
durables (the informer's ``consumer_label`` pattern).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from ..data.sources.webhook_router import (
    INBOUND_STREAM_NAME,
    INBOUND_SUBJECT_ROOT,
    decode_inbound_envelope,
)

logger = logging.getLogger(__name__)


DEFAULT_CONSUMER_DURABLE = "legba-runtime-inbound-drain"
INBOUND_SUBJECT_FILTER = f"{INBOUND_SUBJECT_ROOT}.>"

# Fetch-loop tuning (mirror the informer). A small batch keeps latency down; a
# short timeout keeps the loop responsive to stop().
_FETCH_BATCH = 32
_FETCH_TIMEOUT_SECONDS = 1.0

# Consumer tuning. ``ack_wait`` must cover a full ingest + write + publish per
# envelope (a webhook POST is one event, its own short transaction). Generous
# ``max_deliver`` so a legitimate envelope for a source whose actor has not yet
# activated at boot is not term()-ed before activation completes.
_ACK_WAIT_SECONDS = 60.0
_MAX_DELIVER = 5


@dataclass
class DrainStats:
    """Lightweight counters surfaced for ops + tests."""

    messages_received: int = 0
    written: int = 0            # signals emitted via ingest_and_emit
    acked: int = 0             # envelopes acked after a successful write
    dead_lettered: int = 0     # term()'d (unparseable / unauthorized / exhausted)
    nak_redeliver: int = 0     # nak()'d for a transient retry
    handler_missing: int = 0   # source actor not yet registered
    parse_errors: int = 0      # envelope decode failures
    transient_errors: int = 0  # ingest raised a non-terminal error
    ack_errors: int = 0
    fetch_errors: int = 0
    rebinds: int = 0
    last_subject: str | None = field(default=None)


def _num_delivered(msg: Any) -> int:
    """Best-effort delivery count from the JetStream message metadata.

    Defensive: a fake message in a unit rig (or a metadata-less message) is
    treated as a first delivery so a single transient failure naks rather than
    dead-letters.
    """
    try:
        return int(msg.metadata.num_delivered)
    except Exception:
        return 1


class InboundWebhookDrain:
    """Durable pull consumer that drains ``legba.inbound.>`` → the ingest path.

    Usage::

        store = NatsStore.from_env(); await store.connect()
        await store.ensure_stream("legba_inbound", ["legba.inbound.>"],
                                  retention="workqueue", max_msgs=..., ...)
        drain = InboundWebhookDrain(store, webhook_router)
        await drain.start()
        ...
        await drain.stop()

    The caller owns the :class:`NatsStore` lifecycle + provisions the stream
    (bring-up does both). ``router`` is the process
    :class:`legba.data.sources.webhook_router.InboundWebhookRouter` used to
    resolve the source handler for each envelope's ``source_id``.
    """

    def __init__(
        self,
        nats_store: Any,
        router: Any,
        *,
        stream: str = INBOUND_STREAM_NAME,
        subject_filter: str = INBOUND_SUBJECT_FILTER,
        durable: str = DEFAULT_CONSUMER_DURABLE,
        max_deliver: int = _MAX_DELIVER,
        ack_wait_seconds: float = _ACK_WAIT_SECONDS,
    ) -> None:
        self._store = nats_store
        self._router = router
        self._stream = stream
        self._subject_filter = subject_filter
        self._durable = durable
        self._max_deliver = max_deliver
        self._ack_wait_seconds = ack_wait_seconds
        self._stopped = False
        self._task: asyncio.Task | None = None
        self._sub: Any | None = None
        self._consecutive_errors = 0
        # (base, cap) seconds for the re-bind backoff after a fetch error;
        # overridable in tests to keep the self-heal regression fast.
        self._fetch_error_backoff: tuple[float, float] = (1.0, 30.0)
        self.stats = DrainStats()

    @property
    def consumer_name(self) -> str:
        return self._durable

    async def start(self) -> None:
        """Bind the durable pull consumer and start the fetch loop (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._consecutive_errors = 0
        await self._bind()
        logger.info(
            "inbound_drain.start stream=%s subject_filter=%s consumer=%s",
            self._stream, self._subject_filter, self.consumer_name,
        )
        self._task = asyncio.create_task(
            self._fetch_loop(), name="legba-runtime-inbound-drain",
        )

    async def _bind(self, *, rebind: bool = False) -> None:
        """(Re)create the durable pull consumer + subscription.

        ``add_consumer`` upserts on the durable name — first ``start()`` creates
        it, and a self-heal ``rebind=True`` re-creates it when the server dropped
        it (a non-timeout ``fetch()`` 503, exactly the informer's incident
        shape). ``DeliverPolicy.ALL`` replays buffered/un-acked envelopes on a
        restart — the drain has no other catch-up path.
        """
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

        if rebind and self._sub is not None:
            try:
                await self._sub.unsubscribe()
            except Exception:
                pass
            self._sub = None

        cfg = ConsumerConfig(
            durable_name=self._durable,
            filter_subject=self._subject_filter,
            ack_policy=AckPolicy.EXPLICIT,
            deliver_policy=DeliverPolicy.ALL,
            ack_wait=self._ack_wait_seconds,
            max_deliver=self._max_deliver,
        )
        try:
            await self._store.js.add_consumer(stream=self._stream, config=cfg)
        except Exception as exc:
            logger.debug(
                "inbound_drain.consumer.add err=%s (likely already exists)", exc,
            )
        self._sub = await self._store.js.pull_subscribe(
            subject=self._subject_filter,
            durable=self._durable,
            stream=self._stream,
        )

    async def stop(self) -> None:
        """Stop the fetch loop and unsubscribe (best-effort)."""
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._sub is not None:
            try:
                await self._sub.unsubscribe()
            except Exception:
                pass
            self._sub = None

    async def _fetch_loop(self) -> None:
        try:
            while not self._stopped:
                try:
                    msgs = await self._sub.fetch(
                        _FETCH_BATCH, timeout=_FETCH_TIMEOUT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    continue
                except Exception as exc:
                    err_name = type(exc).__name__
                    if "Timeout" in err_name or "FetchTimeout" in err_name:
                        continue
                    # A real fetch error means the pull subscription is wedged —
                    # most often the durable was dropped server-side (a 503).
                    # RE-BIND rather than retrying the dead subscription forever,
                    # with exponential backoff + rate-limited logging (the
                    # informer's self-heal shape / 2026-06-11 incident).
                    self.stats.fetch_errors += 1
                    self._consecutive_errors += 1
                    base, cap = self._fetch_error_backoff
                    backoff = min(cap, base * 2 ** min(self._consecutive_errors - 1, 5))
                    if self._consecutive_errors == 1 or self._consecutive_errors % 30 == 0:
                        logger.warning(
                            "inbound_drain.fetch.error err=%s consecutive=%d — "
                            "re-binding consumer (backoff=%.1fs)",
                            exc, self._consecutive_errors, backoff,
                        )
                    await asyncio.sleep(backoff)
                    if self._stopped:
                        return
                    try:
                        await self._bind(rebind=True)
                        self.stats.rebinds += 1
                    except Exception as bind_exc:
                        logger.warning("inbound_drain.rebind.error err=%s", bind_exc)
                    continue
                # Successful fetch — clear the error streak.
                self._consecutive_errors = 0
                for msg in msgs:
                    await self._handle_message(msg)
        except asyncio.CancelledError:
            return

    async def _handle_message(self, msg: Any) -> None:
        self.stats.messages_received += 1
        subject = getattr(msg, "subject", "") or ""
        self.stats.last_subject = subject

        # Decode the RAW envelope. A hard-unparseable envelope is dead-lettered
        # immediately — replay cannot fix bad bytes.
        try:
            env = decode_inbound_envelope(msg.data)
            source_id = env["source_id"]
            body = env["body"]
            headers = env["headers"]
        except Exception as exc:
            self.stats.parse_errors += 1
            self.stats.dead_lettered += 1
            logger.warning(
                "inbound_drain.deadletter reason=unparseable_envelope "
                "subject=%s err=%s", subject, exc,
            )
            await self._term(msg)
            return

        # Resolve the source handler. A not-yet-activated actor at boot resolves
        # to None → NAK/redeliver (transient); term()'d only after max_deliver.
        handler = self._router.get_handler(source_id)
        if handler is None:
            self.stats.handler_missing += 1
            await self._nak_or_term(
                msg, reason="handler_not_registered", source_id=source_id,
            )
            return

        # Ingest + write OFF the request path. ACK ONLY after a successful write.
        try:
            count = await handler.ingest_and_emit(body, headers)
        except (ValueError, PermissionError) as exc:
            # Terminal — the payload is unparseable or the token re-verify
            # failed. Dead-letter; replay will not help.
            self.stats.dead_lettered += 1
            logger.warning(
                "inbound_drain.deadletter reason=unparseable_or_unauthorized "
                "source_id=%s err=%s", source_id, exc,
            )
            await self._term(msg)
            return
        except Exception as exc:
            # Transient (DB blip, etc.) — NAK/redeliver, term after max_deliver.
            self.stats.transient_errors += 1
            logger.warning(
                "inbound_drain.ingest.transient source_id=%s err=%s",
                source_id, exc,
            )
            await self._nak_or_term(
                msg, reason="ingest_transient", source_id=source_id,
            )
            return

        self.stats.written += count
        try:
            await msg.ack()
            self.stats.acked += 1
        except Exception as exc:
            self.stats.ack_errors += 1
            logger.warning(
                "inbound_drain.ack.error source_id=%s err=%s", source_id, exc,
            )

    async def _nak_or_term(self, msg: Any, *, reason: str, source_id: str) -> None:
        """NAK for redelivery, or term()/dead-letter once max_deliver is hit."""
        delivered = _num_delivered(msg)
        if delivered >= self._max_deliver:
            self.stats.dead_lettered += 1
            logger.warning(
                "inbound_drain.deadletter reason=%s source_id=%s delivered=%d "
                "(max_deliver=%d reached)",
                reason, source_id, delivered, self._max_deliver,
            )
            await self._term(msg)
            return
        self.stats.nak_redeliver += 1
        logger.info(
            "inbound_drain.nak reason=%s source_id=%s delivered=%d",
            reason, source_id, delivered,
        )
        await self._nak(msg)

    async def _term(self, msg: Any) -> None:
        try:
            await msg.term()
        except Exception as exc:
            logger.warning("inbound_drain.term.error err=%s", exc)

    async def _nak(self, msg: Any) -> None:
        try:
            await msg.nak()
        except Exception as exc:
            logger.warning("inbound_drain.nak.error err=%s", exc)


__all__ = [
    "DEFAULT_CONSUMER_DURABLE",
    "INBOUND_SUBJECT_FILTER",
    "DrainStats",
    "InboundWebhookDrain",
]
