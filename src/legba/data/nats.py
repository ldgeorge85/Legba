# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.nats — async NATS client + JetStream helpers.

Per `design/legba_storage_layout.md` §2.7 / §6: JetStream is the durable
substrate-coordination surface; analyst subscription is a long-lived durable
consumer pattern.

The L-001 substrate factor wires the connection. Stream layout (per-target,
per-analyst, dead-letter, control) is defined by L-103 (runtime spec); this
module exposes the helpers L-110/L-111 use to declare them.
"""

from __future__ import annotations

import logging
import re
from typing import Any

try:
    import nats
    from nats.aio.client import Client as NATSClient
    from nats.js import JetStreamContext
    from nats.js.api import (
        AckPolicy,
        ConsumerConfig,
        ConsumerInfo,
        DeliverPolicy,
        DiscardPolicy,
        RetentionPolicy,
        StreamConfig,
    )
except Exception:  # pragma: no cover
    nats = None  # type: ignore[assignment]
    NATSClient = None  # type: ignore[assignment]
    JetStreamContext = None  # type: ignore[assignment]
    StreamConfig = None  # type: ignore[assignment]
    ConsumerConfig = None  # type: ignore[assignment]
    ConsumerInfo = None  # type: ignore[assignment]
    DiscardPolicy = None  # type: ignore[assignment]
    RetentionPolicy = None  # type: ignore[assignment]
    AckPolicy = None  # type: ignore[assignment]
    DeliverPolicy = None  # type: ignore[assignment]

from .config import NatsConfig

logger = logging.getLogger(__name__)


# ===========================================================================
# Coarse signal-subject taxonomy (source-first pivot — P-08 / PIVOT §6.1).
# ===========================================================================
#
# JetStream filters SUBJECTS, not arbitrary JSON. So a signal's subject encodes
# only the COARSE routing axes — tenant / source / modality / event-class —
# and exact matching is done downstream (SQL ``WHERE`` over the indexed signals
# table + the Starlark residual on the narrowed set). Never try to express an
# arbitrary subscription predicate as a NATS subject (PIVOT §4.4 / §6.1).
#
# Layout (six tokens, all '.'-free per NATS rules):
#
#     legba.signals.<tenant>.<source_token>.<modality>.<event_class>
#
#   * tenant       — SourceDescriptor.scope.owner_tenant (the tenancy seam).
#   * source_token — SourceDescriptor.id with '.'→'_' (source ids carry dots,
#                    which are subject separators, so they're flattened).
#   * modality     — Signal.modality (text/image/audio/video/structured/binary).
#   * event_class  — coarse class of the signal; default ``raw`` for a raw
#                    source row, ``derived`` for a job/analyst-produced row.
#
# A subscribing target builds a SET of subject *filters* (one per coarse
# combination its structured filter narrows to, or a wildcard where a coarse
# axis is unconstrained) and binds them onto ONE per-target aggregated consumer
# (PIVOT §6.1 — per-target, not per-(target,source)).

SIGNAL_SUBJECT_ROOT = "legba.signals"

# Single aggregated raw-pool stream — one stream covers every source's signals
# subject; per-target consumers attach subject-filtered (PIVOT §6.1). A
# per-source override is possible (lossless sources get their own work-queue
# stream) but the default is the shared interest stream.
SIGNAL_STREAM_NAME = "legba_signals"
SIGNAL_STREAM_SUBJECTS = [f"{SIGNAL_SUBJECT_ROOT}.>"]

_SUBJECT_TOKEN_DISALLOWED = re.compile(r"[ \t\n.*>/\\]")


def subject_token(value: str) -> str:
    """Flatten an arbitrary id into a single NATS subject token.

    Source ids carry dots (``source.reuters.world``) which are subject
    separators; '.' and the other reserved chars ('*', '>', whitespace,
    path separators) are replaced with '_' so the id becomes one token.
    """
    if not value:
        return "_"
    return _SUBJECT_TOKEN_DISALLOWED.sub("_", value)


def signal_subject(
    *,
    tenant: str,
    source_id: str,
    modality: str,
    event_class: str = "raw",
) -> str:
    """Compose the coarse publish subject for one signal (PIVOT §6.1).

    ``legba.signals.<tenant>.<source_token>.<modality>.<event_class>``.
    """
    return (
        f"{SIGNAL_SUBJECT_ROOT}."
        f"{subject_token(tenant)}."
        f"{subject_token(source_id)}."
        f"{subject_token(modality)}."
        f"{subject_token(event_class)}"
    )


def signal_subject_filter(
    *,
    tenant: str | None = None,
    source_id: str | None = None,
    modality: str | None = None,
    event_class: str | None = None,
) -> str:
    """Compose a coarse subject FILTER with '*' for unconstrained axes.

    A ``None`` axis becomes the single-token wildcard ``*`` (a coarse axis
    the subscription does not constrain). Exact matching beyond these four
    coarse axes is the SQL ``WHERE`` + Starlark residual, never the subject.
    """
    return (
        f"{SIGNAL_SUBJECT_ROOT}."
        f"{subject_token(tenant) if tenant else '*'}."
        f"{subject_token(source_id) if source_id else '*'}."
        f"{subject_token(modality) if modality else '*'}."
        f"{subject_token(event_class) if event_class else '*'}"
    )


# ---------------------------------------------------------------------------
# Backfill event-class — the manual signals lane (S4-T4)
# ---------------------------------------------------------------------------
#
# A *backfilled* signal (a manually-ingested, backdated observation loaded via
# :mod:`legba.data.seed.manual_batch`) rides the NORMAL signal contract —
# baseline enrichment, entity/geo, fan-out, dedupe — but it is a HISTORICAL
# observation, not "what just happened". So it publishes on the ``backfill``
# event-class subject and carries ``event_class=backfill`` in its ``payload``
# (the persistence home — the ``signals`` table has no ``event_class`` column;
# ``published_at`` already lives in payload beside it). Consequences:
#
#   * Every UNIT's fresh REACTIVE-window read EXCLUDES it — a months-old event
#     must never surface as a fresh signal in a unit's reactive slice. That is
#     exactly the one predicate below, ANDed into each window read.
#   * The ACCUMULATION / fact / grounding / dedupe / entity-resolution paths
#     KEEP it — a backfill SHOULD inform the knowledge those paths accumulate.
BACKFILL_EVENT_CLASS = "backfill"

# The one-line ``WHERE`` fragment every fresh-slice window read ANDs in to keep
# backfill out of a unit's reactive slice. ``payload->>'event_class'`` is NULL
# for a normal signal (no such key) → ``NULL IS DISTINCT FROM 'backfill'`` is
# TRUE → the row is kept; a backfill row's value equals ``'backfill'`` →
# ``'backfill' IS DISTINCT FROM 'backfill'`` is FALSE → the row is excluded.
SIGNALS_EXCLUDE_BACKFILL_SQL = "(payload->>'event_class') IS DISTINCT FROM 'backfill'"


class NatsStore:
    def __init__(self, cfg: NatsConfig):
        if nats is None:  # pragma: no cover
            raise RuntimeError("nats-py is not installed")
        self._cfg = cfg
        self._nc: NATSClient | None = None
        self._js: JetStreamContext | None = None

    @classmethod
    def from_env(cls) -> "NatsStore":
        return cls(NatsConfig.from_env())

    @property
    def cfg(self) -> NatsConfig:
        return self._cfg

    @property
    def nc(self) -> "NATSClient":
        if self._nc is None:
            raise RuntimeError("NatsStore not connected")
        return self._nc

    @property
    def js(self) -> "JetStreamContext":
        if self._js is None:
            raise RuntimeError("NatsStore not connected")
        return self._js

    async def connect(self) -> None:
        if self._nc is not None and self._nc.is_connected:
            return
        connect_kwargs: dict[str, Any] = {
            "servers": [self._cfg.url],
            "connect_timeout": self._cfg.connect_timeout,
        }
        if self._cfg.user and self._cfg.password:
            connect_kwargs["user"] = self._cfg.user
            connect_kwargs["password"] = self._cfg.password
        if self._cfg.creds_file:
            connect_kwargs["user_credentials"] = self._cfg.creds_file
        # B-1: server-wide token authorization (LEGBA_NATS_TOKEN). None/empty
        # keeps the unauthenticated dev/pre-cutover behaviour.
        if self._cfg.token:
            connect_kwargs["token"] = self._cfg.token
        self._nc = await nats.connect(**connect_kwargs)
        self._js = self._nc.jetstream()

    async def close(self) -> None:
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception:
                pass
            try:
                await self._nc.close()
            except Exception:
                pass
            self._nc = None
            self._js = None

    # ------------------------------------------------------------------
    # JetStream helpers
    # ------------------------------------------------------------------

    async def ensure_stream(
        self,
        name: str,
        subjects: list[str],
        *,
        retention: str = "limits",
        max_age_seconds: int = 0,
        max_msgs: int = -1,
        discard: str = "old",
    ) -> bool:
        """Idempotently create a JetStream stream. Returns True if created.

        ``discard`` selects the full-stream policy (only bites once ``max_msgs``/
        ``max_age`` is reached): ``"old"`` (JetStream default) evicts the OLDEST
        message to accept a new publish; ``"new"`` REJECTS the new publish (the
        publisher's ``js.publish`` raises) so an accept-and-enqueue front can turn
        a full buffer into honest backpressure (503) rather than silently dropping
        already-accepted work. Used ``discard="new"`` for ``legba_inbound`` (S1).
        """
        try:
            await self.js.stream_info(name)
            return False
        except Exception:
            pass
        await self.js.add_stream(
            StreamConfig(
                name=name,
                subjects=subjects,
                retention={
                    "limits": RetentionPolicy.LIMITS,
                    "interest": RetentionPolicy.INTEREST,
                    "workqueue": RetentionPolicy.WORK_QUEUE,
                }.get(retention.lower(), RetentionPolicy.LIMITS),
                discard={
                    "old": DiscardPolicy.OLD,
                    "new": DiscardPolicy.NEW,
                }.get(discard.lower(), DiscardPolicy.OLD),
                # nats-py StreamConfig.max_age is in seconds (float),
                # not nanoseconds — earlier multiplication was a bug for
                # any non-zero value. Surfaced by L-124 NATS handler.
                max_age=max_age_seconds if max_age_seconds else 0,
                max_msgs=max_msgs,
            )
        )
        return True

    async def publish_json(self, subject: str, payload: bytes) -> None:
        await self.js.publish(subject, payload)

    async def publish_core(self, subject: str, payload: bytes) -> None:
        """Core-NATS publish (fire-and-forget, no JetStream stream/ack).

        For interest-only subjects that have NO JetStream stream — chiefly
        ``legba.alerts.*`` (the alert fan-out for live subscribers: UI feed,
        the audit writer, the liveness watchdog). ``publish_json`` cannot be
        used there: it awaits a stream publish-ack and raises
        ``NoStreamResponseError`` when no stream covers the subject — which is
        exactly why alert delivery was silently failing (the alert output sink
        AND the watchdog both published via JetStream onto a streamless
        subject, so 0 alerts ever reached a subscriber). Core publish has no
        ack round-trip; if no subscriber is listening the message is simply
        dropped, which is the correct semantics for an interest-only alert
        bus.
        """
        await self.nc.publish(subject, payload)

    # ------------------------------------------------------------------
    # Per-target aggregated consumers (PIVOT §6.1 — P-08 fan-out)
    # ------------------------------------------------------------------

    async def ensure_durable_consumer(
        self,
        stream: str,
        durable: str,
        *,
        filter_subjects: list[str],
        deliver_policy: str = "all",
        ack_policy: str = "explicit",
        max_ack_pending: int = 1000,
    ) -> "ConsumerInfo":
        """Idempotently create/refresh a per-target durable PULL consumer.

        ONE consumer per target (PIVOT §6.1: "per-target aggregated
        consumers, not per-(target, source)") subject-filtered onto the
        coarse axes the target's subscriptions narrow to. ``filter_subjects``
        is the (de-duplicated) set of coarse subject filters; nats-py maps it
        to JetStream's multi-subject filter. A single ``*`` everywhere means
        "everything in the stream".

        Returns the live :class:`ConsumerInfo` (carries ``num_pending`` =
        lag). If the durable already exists with a DIFFERENT filter set we
        recreate it (filter changes aren't an in-place update in JetStream).
        """
        deliver = {
            "all": DeliverPolicy.ALL,
            "new": DeliverPolicy.NEW,
            "last": DeliverPolicy.LAST,
        }.get(deliver_policy.lower(), DeliverPolicy.ALL)
        ack = {
            "explicit": AckPolicy.EXPLICIT,
            "all": AckPolicy.ALL,
            "none": AckPolicy.NONE,
        }.get(ack_policy.lower(), AckPolicy.EXPLICIT)

        wanted = sorted(set(filter_subjects)) or [f"{SIGNAL_SUBJECT_ROOT}.>"]
        # JetStream: a single subject goes in `filter_subject`, multiple in
        # `filter_subjects` (and the two are mutually exclusive server-side).
        cfg_kwargs: dict[str, Any] = {
            "durable_name": durable,
            "deliver_policy": deliver,
            "ack_policy": ack,
            "max_ack_pending": max_ack_pending,
        }
        if len(wanted) == 1:
            cfg_kwargs["filter_subject"] = wanted[0]
        else:
            cfg_kwargs["filter_subjects"] = wanted

        existing: ConsumerInfo | None = None
        try:
            existing = await self.js.consumer_info(stream, durable)
        except Exception:
            existing = None

        if existing is not None:
            cur_filters = sorted(
                set(existing.config.filter_subjects or [])
                | ({existing.config.filter_subject} if existing.config.filter_subject else set())
            )
            if cur_filters == wanted:
                return existing
            # Filter set changed — recreate (JetStream rejects in-place
            # filter edits on a durable).
            try:
                await self.js.delete_consumer(stream, durable)
            except Exception as exc:  # pragma: no cover
                logger.warning("delete_consumer %s/%s failed: %s", stream, durable, exc)

        await self.js.add_consumer(stream, ConsumerConfig(**cfg_kwargs))
        return await self.js.consumer_info(stream, durable)

    async def consumer_lag(self, stream: str, durable: str) -> dict[str, Any]:
        """Return the lag/health snapshot for a per-target consumer.

        ``num_pending`` is the headline lag (messages on the stream the
        consumer has not yet delivered) — the acceptance criterion for
        "per-target consumer lag is observable". The rest round out the
        slow-consumer + stream-growth monitoring mandated in PIVOT §6.1.
        """
        info = await self.js.consumer_info(stream, durable)
        return {
            "stream": stream,
            "durable": durable,
            "num_pending": info.num_pending,            # lag (undelivered)
            "num_ack_pending": info.num_ack_pending,    # delivered, unacked
            "num_redelivered": info.num_redelivered,
            "num_waiting": info.num_waiting,
            "delivered_stream_seq": (
                info.delivered.stream_seq if info.delivered else None
            ),
            "ack_floor_stream_seq": (
                info.ack_floor.stream_seq if info.ack_floor else None
            ),
        }

    async def stream_growth(self, stream: str) -> dict[str, Any]:
        """Return the stream-growth snapshot (PIVOT §6.1 monitoring)."""
        info = await self.js.stream_info(stream)
        return {
            "stream": stream,
            "messages": info.state.messages,
            "bytes": info.state.bytes,
            "first_seq": info.state.first_seq,
            "last_seq": info.state.last_seq,
            "consumer_count": info.state.consumer_count,
        }
