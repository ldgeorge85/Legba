# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NATS → ReconcileLoop event-driven informer (Phase 5 hardening item 1).

The runtime previously relied on the :class:`ReconcileLoop` periodic
resync (5 min) to discover descriptor changes. With the runtime event
streams provisioned (see :mod:`legba.data.registry.streams`) the
registry's lifecycle publishes become consumable; this module wires
those publishes to :meth:`ReconcileLoop.enqueue` so descriptor changes
propagate to actor state in <1s instead of ~5min.

Design (per the post-bring-up review §3 + Lewis's decisions):

  * **Durable pull consumer.** Survives runtime restarts; consumer name
    is stable so ops can inspect it via ``nats consumer ls``.
  * **Explicit ack per message.** The reconcile enqueue is fast and
    idempotent (the work queue dedupes within the loop), so we ack as
    soon as we hand off the descriptor_id.
  * **No replay across restarts.** ``DeliverPolicy.NEW`` — the periodic
    resync covers catchup, the informer only handles fresh events.
  * **Interest retention.** Set on the stream (in
    :mod:`legba.data.registry.streams`); auto-cleanup once we ack.

Subject grammar (per :mod:`legba.data.registry.events`):

    ``descriptor.<action>.<family>.<descriptor_id>``

The informer extracts ``family`` + ``descriptor_id`` from the subject
tokens, calls ``loop.enqueue(descriptor_id, reason="nats_event:<subject>")``,
and acks.

Consumer name format: ``legba-runtime-reconcile-{consumer_label}`` where
``consumer_label`` defaults to ``"informer"``. Ops can override per host
when running multiple runtime instances against the same NATS cluster
(each instance gets its own durable so messages fan out across consumers
under interest retention).

S-2 — the STACK-COMPONENT twin
------------------------------

The registry publishes a second lifecycle event class the runtime never
consumed: ``stack.component.>`` on ``LEGBA_STACK_EVENTS`` (see
:mod:`legba.data.registry.stack_events`). :class:`NatsStackComponentInformer`
binds it with the same durable-pull machinery and evicts the process-lifetime
LLM handler caches, so an operator's stack-component PUT takes effect on the
next call instead of the next container recreate. Both informers share
:class:`_NatsSubjectInformer` — the fetch loop's re-bind/backoff self-heal is
hard-won incident logic (the 2026-06-11 dropped-consumer outage) and must not
be duplicated per subject family.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from ..data.nats import NatsStore
from ..data.registry.streams import (
    DESCRIPTOR_EVENTS_STREAM,
    STACK_EVENTS_STREAM,
)

if TYPE_CHECKING:  # pragma: no cover
    from .reconcile import ReconcileLoop

logger = logging.getLogger(__name__)

DEFAULT_CONSUMER_LABEL = "informer"
DEFAULT_CONSUMER_DURABLE_PREFIX = "legba-runtime-reconcile"
DESCRIPTOR_SUBJECT_FILTER = "descriptor.>"

#: S-2 — the stack-component informer's durable prefix + subject filter. A
#: SEPARATE durable from the descriptor informer's: the two bind different
#: streams, and one wedged consumer must not stall the other.
DEFAULT_STACK_CONSUMER_DURABLE_PREFIX = "legba-runtime-stack"
STACK_SUBJECT_FILTER = "stack.component.>"

#: Stack actions that CANNOT change a component's body, and so must not evict.
#: ``health_changed`` is published by the latching health prober on every
#: transition — it carries a liveness verdict, never config. Evicting on it
#: would let a flapping endpoint churn the handler cache (rebuilding an
#: httpx client + re-resolving vault credentials) for no config delta, which
#: is exactly the kind of self-inflicted load a cache exists to prevent.
#: Every OTHER action (registered/updated/configured/activated/paused/
#: resumed/retired/promoted/rolled_back) either changes the body or changes
#: whether the component should be served at all, so all of them evict.
_NON_EVICTING_STACK_ACTIONS = frozenset({"health_changed"})

# Fetch-loop tuning. The pull consumer fetches in batches; a small
# batch keeps latency down (we want events processed within ~1s of the
# registry write), a short timeout keeps the fetch loop responsive to
# stop() calls.
_FETCH_BATCH = 32
_FETCH_TIMEOUT_SECONDS = 1.0


@dataclass
class InformerStats:
    """Lightweight counters surfaced for ops + tests.

    * ``messages_received`` — total raw messages pulled from the stream.
    * ``enqueued`` — successful ``ReconcileLoop.enqueue`` calls.
    * ``parse_errors`` — subject parse failures (skipped + acked).
    * ``ack_errors`` — ack call failures (logged, not retried).
    * ``evicted`` — S-2 stack informer only: cache entries dropped.
    """

    messages_received: int = 0
    enqueued: int = 0
    parse_errors: int = 0
    ack_errors: int = 0
    fetch_errors: int = 0
    rebinds: int = 0
    evicted: int = 0
    last_subject: str | None = field(default=None)


class _NatsSubjectInformer:
    """Durable-pull consumer over ONE subject family, with self-healing binds.

    The shared half of both informers: consumer creation, the fetch loop, the
    re-bind/backoff self-heal, ack, and the stats counters. Subclasses supply
    the durable prefix (:attr:`_durable_prefix`) and the per-message action
    (:meth:`_dispatch`); everything above the message is identical because the
    failure modes are identical.

    Not exported — construct :class:`NatsReconcileInformer` or
    :class:`NatsStackComponentInformer`.
    """

    #: Durable-name prefix, joined to the consumer label with a ``-``.
    _durable_prefix: str = DEFAULT_CONSUMER_DURABLE_PREFIX

    def __init__(
        self,
        nats_store: NatsStore,
        *,
        stream: str,
        subject_filter: str,
        consumer_label: str = DEFAULT_CONSUMER_LABEL,
    ) -> None:
        self._store = nats_store
        self._consumer_label = consumer_label
        self._stream = stream
        self._subject_filter = subject_filter
        self._stopped = False
        self._task: asyncio.Task | None = None
        self._sub: Any | None = None
        self._consecutive_errors = 0
        # (base, cap) seconds for the re-bind backoff after a fetch error;
        # overridable in tests to keep the self-heal regression fast.
        self._fetch_error_backoff: tuple[float, float] = (1.0, 30.0)
        self.stats = InformerStats()

    # ---- subclass hook ---------------------------------------------------

    def _dispatch(self, subject: str) -> bool:
        """Act on one message's subject. ``True`` = handled, ``False`` = parse
        failure (counted in ``stats.parse_errors``; the message is still
        acked, since replaying an unparseable subject cannot help)."""
        raise NotImplementedError

    # ---- shared consumer machinery ---------------------------------------

    @property
    def consumer_name(self) -> str:
        """Durable consumer name — stable across restarts, inspectable via ``nats``."""
        return f"{self._durable_prefix}-{self._consumer_label}"

    async def start(self) -> None:
        """Bind the durable pull consumer and start the fetch loop.

        Idempotent — a second ``start()`` is a no-op while the first is
        running.
        """
        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        self._consecutive_errors = 0
        await self._bind()
        logger.info(
            "nats_informer.start stream=%s subject_filter=%s consumer=%s",
            self._stream, self._subject_filter, self.consumer_name,
        )
        self._task = asyncio.create_task(
            self._fetch_loop(), name=f"legba-runtime-nats-{self._consumer_label}",
        )

    async def _bind(self, *, rebind: bool = False) -> None:
        """(Re)create the durable pull consumer + subscription.

        ``add_consumer`` upserts on the durable name, so this both creates the
        consumer on first ``start()`` and **re-creates** it when the server has
        dropped it — the self-heal path. A dropped consumer manifests as a
        non-timeout ``fetch()`` error (a 503 ServiceUnavailable); without the
        re-bind the fetch loop retried the dead subscription forever (a ~3h /
        10k-line incident on 2026-06-11; the exact server-side reap cause was
        not isolated, so the self-heal is robust to consumer loss from *any*
        cause — reap, stream re-provision, leader change). On ``rebind`` the
        stale subscription is unsubscribed first.

        ``DeliverPolicy.NEW`` means events published during the outage are not
        replayed — intentional: the ``ReconcileLoop`` periodic resync (≤5 min)
        is the catch-up path; the informer only carries fresh events.
        """
        # Import locally so reconcile.py doesn't have to import nats.
        from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy

        if rebind and self._sub is not None:
            try:
                await self._sub.unsubscribe()
            except Exception:
                pass
            self._sub = None

        cfg = ConsumerConfig(
            durable_name=self.consumer_name,
            filter_subject=self._subject_filter,
            ack_policy=AckPolicy.EXPLICIT,
            deliver_policy=DeliverPolicy.NEW,
            ack_wait=30.0,
            max_deliver=5,
        )
        # Idempotently create/update — server upserts on matching durable.
        try:
            await self._store.js.add_consumer(stream=self._stream, config=cfg)
        except Exception as exc:
            # Existing consumer is fine; surface anything else.
            logger.debug(
                "nats_informer.consumer.add err=%s (likely already exists)", exc,
            )
        self._sub = await self._store.js.pull_subscribe(
            subject=self._subject_filter,
            durable=self.consumer_name,
            stream=self._stream,
        )

    async def stop(self) -> None:
        """Stop the fetch loop and unsubscribe.

        Best-effort — exceptions during shutdown are logged + swallowed
        because the substrate may already be torn down.
        """
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
                    # The nats-py client throws non-asyncio TimeoutErrors
                    # too; treat the "no messages" case as a no-op and
                    # re-loop so stop() can break us out.
                    err_name = type(exc).__name__
                    if "Timeout" in err_name or "FetchTimeout" in err_name:
                        continue
                    # A real fetch error means the pull subscription is wedged —
                    # most often the durable consumer was dropped server-side (a
                    # 503 ServiceUnavailable). RE-BIND the consumer rather than
                    # retrying the dead subscription forever, with exponential
                    # backoff + rate-limited logging so a persistent outage can't
                    # flood (the pre-fix loop spun ~1/s for 3h = 10k log lines).
                    self.stats.fetch_errors += 1
                    self._consecutive_errors += 1
                    base, cap = self._fetch_error_backoff
                    backoff = min(cap, base * 2 ** min(self._consecutive_errors - 1, 5))
                    if self._consecutive_errors == 1 or self._consecutive_errors % 30 == 0:
                        logger.warning(
                            "nats_informer.fetch.error err=%s consecutive=%d — "
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
                        logger.warning("nats_informer.rebind.error err=%s", bind_exc)
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
        try:
            handled = self._dispatch(subject)
        except Exception as exc:  # noqa: BLE001 — one bad message must not
            # kill the fetch loop; that would silently stop ALL propagation
            # on this subject family (the exact silent-failure class S-2 and
            # S-1 exist to remove). Count it as a parse error and ack on.
            handled = False
            logger.warning(
                "nats_informer.dispatch.error subject=%s err=%s", subject, exc,
            )
        if not handled:
            self.stats.parse_errors += 1
        # Ack regardless — even if we failed to parse, replaying it
        # won't help. Interest retention will then drop the message.
        try:
            await msg.ack()
        except Exception as exc:
            self.stats.ack_errors += 1
            logger.warning(
                "nats_informer.ack.error subject=%s err=%s",
                subject, exc,
            )


class NatsReconcileInformer(_NatsSubjectInformer):
    """Pull-consumer informer that bridges NATS events → ReconcileLoop.

    Usage::

        store = NatsStore.from_env()
        await store.connect()
        loop = ReconcileLoop(...)
        await loop.start()
        informer = NatsReconcileInformer(store, loop)
        await informer.start()
        ...
        await informer.stop()
        await loop.stop()

    The caller owns the :class:`NatsStore` lifecycle (connect / close).
    The informer creates its own durable consumer on the
    ``LEGBA_DESCRIPTOR_EVENTS`` stream; the stream itself must be
    provisioned out-of-band (the registry server does this on startup —
    see :func:`legba.data.registry.streams.ensure_runtime_event_streams`).
    """

    _durable_prefix = DEFAULT_CONSUMER_DURABLE_PREFIX

    def __init__(
        self,
        nats_store: NatsStore,
        reconcile_loop: "ReconcileLoop",
        *,
        consumer_label: str = DEFAULT_CONSUMER_LABEL,
        stream: str = DESCRIPTOR_EVENTS_STREAM,
        subject_filter: str = DESCRIPTOR_SUBJECT_FILTER,
    ) -> None:
        super().__init__(
            nats_store,
            stream=stream,
            subject_filter=subject_filter,
            consumer_label=consumer_label,
        )
        self._loop = reconcile_loop

    def _dispatch(self, subject: str) -> bool:
        descriptor_id = _parse_descriptor_id_from_subject(subject)
        if descriptor_id is None:
            logger.warning(
                "nats_informer.parse.skip subject=%s (could not extract descriptor_id)",
                subject,
            )
            return False
        self._loop.enqueue(descriptor_id, reason=f"nats_event:{subject}")
        self.stats.enqueued += 1
        logger.debug(
            "nats_informer.enqueued subject=%s descriptor_id=%s",
            subject, descriptor_id,
        )
        return True


class NatsStackComponentInformer(_NatsSubjectInformer):
    """S-2 — ``stack.component.>`` → LLM handler-cache eviction.

    The twin of :class:`NatsReconcileInformer` for the OTHER lifecycle event
    class the registry publishes. Where the descriptor informer propagates a
    descriptor edit into actor state, this one propagates a stack-component
    edit into the process's built-handler caches: the cached handler for the
    changed component is dropped, and the next call rebuilds it from the live
    component row.

    Why it exists: ``_llm_handler_cache`` keys by ``component_id`` with no
    version and had no invalidation, so the operator's 2026-08-01 timeout PUT
    (60→240s, mid-incident) stayed inert for 3.5 h until a container recreate.
    See :mod:`legba.runtime.llm_handler_cache` for the eviction contract (and
    for why eviction drops rather than closes).

    ``evict`` is injected — production passes
    :func:`legba.runtime.llm_handler_cache.evict_llm_handler`; tests pass a
    spy. It returns the number of cache entries dropped, which is what the
    informer logs (0 is the ordinary case for a component this process never
    built a handler for, and is NOT an error).
    """

    _durable_prefix = DEFAULT_STACK_CONSUMER_DURABLE_PREFIX

    def __init__(
        self,
        nats_store: NatsStore,
        *,
        evict: Callable[[str], int],
        consumer_label: str = DEFAULT_CONSUMER_LABEL,
        stream: str = STACK_EVENTS_STREAM,
        subject_filter: str = STACK_SUBJECT_FILTER,
    ) -> None:
        super().__init__(
            nats_store,
            stream=stream,
            subject_filter=subject_filter,
            consumer_label=consumer_label,
        )
        self._evict = evict

    def _dispatch(self, subject: str) -> bool:
        parsed = parse_stack_subject(subject)
        if parsed is None:
            logger.warning(
                "stack_informer.parse.skip subject=%s (could not extract "
                "action/component_id)", subject,
            )
            return False
        action, component_id = parsed
        if action in _NON_EVICTING_STACK_ACTIONS:
            logger.debug(
                "stack_informer.skip subject=%s action=%s (no body change)",
                subject, action,
            )
            return True
        evicted = self._evict(component_id)
        self.stats.enqueued += 1
        self.stats.evicted += evicted
        if evicted:
            logger.info(
                "stack_informer.evicted subject=%s component_id=%s action=%s "
                "handlers=%d — the next LLM call rebuilds from the live "
                "component", subject, component_id, action, evicted,
            )
        else:
            logger.debug(
                "stack_informer.no_cached_handler subject=%s component_id=%s",
                subject, component_id,
            )
        return True


def parse_stack_subject(subject: str) -> tuple[str, str] | None:
    """``(action, component_id)`` from ``stack.component.<action>.<kind>.<id>``.

    Returns ``None`` if the subject doesn't match the grammar. Component ids
    ARE dotted (``llm.primary.openai_compat``), so everything past the 5th
    token rejoins into the id — the reason this can't be a naive ``split``.
    """
    if not subject:
        return None
    tokens = subject.split(".")
    if len(tokens) < 5 or tokens[0] != "stack" or tokens[1] != "component":
        return None
    action = tokens[2]
    component_id = ".".join(tokens[4:])
    if not action or not component_id:
        return None
    return action, component_id


def _parse_descriptor_id_from_subject(subject: str) -> str | None:
    """Extract ``<descriptor_id>`` from ``descriptor.<action>.<family>.<descriptor_id>``.

    Returns ``None`` if the subject doesn't match the grammar.
    """
    if not subject:
        return None
    tokens = subject.split(".")
    if len(tokens) < 4 or tokens[0] != "descriptor":
        return None
    # Subject is ``descriptor.<action>.<family>.<descriptor_id>``; rejoin
    # everything past the 4th token in case an operator id ever contains
    # additional dots (it shouldn't per the events.py validator, but the
    # parse is defensive).
    return ".".join(tokens[3:])


__all__ = [
    "DEFAULT_CONSUMER_LABEL",
    "DEFAULT_STACK_CONSUMER_DURABLE_PREFIX",
    "DESCRIPTOR_SUBJECT_FILTER",
    "STACK_SUBJECT_FILTER",
    "InformerStats",
    "NatsReconcileInformer",
    "NatsStackComponentInformer",
    "parse_stack_subject",
]
