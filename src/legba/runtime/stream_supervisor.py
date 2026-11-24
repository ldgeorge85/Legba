# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SUBSCRIBE supervisor — one connector per active subscribed head (I-4).

WHERE IT RUNS: a small loop in the runtime host, started beside the inbound
webhook drain and the intake drain (``source_first_runtime``), behind
``LEGBA_SUBSCRIBE_SUPERVISOR`` (off by default — a roll changes nothing until
it is switched on, and no subscribed source ships in the tree). Not an actor
and not a workflow engine: each tick reads the ACTIVE ``acquisition:
subscribe`` heads from the registry's table and reconciles the running
connectors against them —

* a head with no running connector gets one (a connector whose task died is
  restarted: the host restarts its supervised tasks);
* a head whose version changed has its connector stopped and a new one
  started (the actor id, and so the cursor row, embeds the version: a new
  head starts as a first connect, under the first-connect age cap — the poll
  path's rule for a new head);
* a connector whose head is gone, paused or retired is stopped (queued events
  are landed, the final window written, the lease released).

Several runtime replicas may run the supervisor: each connector holds its
source's stream only while it holds that source's advisory lease
(:class:`~.stream_connector.PgAdvisoryLease`), so exactly one replica
connects and the others stand by.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any, Awaitable, Callable

from .source_actor import resolve_source_deps
from .stream_connector import PgAdvisoryLease, SubscribeConnector
from .stream_ingest import StreamCore

logger = logging.getLogger("legba.runtime.stream")

__all__ = [
    "SUPERVISOR_ENV",
    "SubscribeSupervisor",
    "start_subscribe_supervisor_if_enabled",
    "subscribe_supervisor_enabled",
]

SUPERVISOR_ENV = "LEGBA_SUBSCRIBE_SUPERVISOR"
_ON = frozenset({"1", "on", "true", "yes"})
_INTERVAL_ENV = "LEGBA_SUBSCRIBE_SUPERVISOR_INTERVAL_SECONDS"

_HEADS_SQL = """
SELECT descriptor_id, body #>> '{identity,version}' AS version
  FROM source_descriptors
 WHERE is_head AND state = 'active' AND body->>'acquisition' = 'subscribe'
"""


def subscribe_supervisor_enabled() -> bool:
    return os.environ.get(SUPERVISOR_ENV, "").strip().lower() in _ON


def _actor_id(source_id: str, version: str) -> str:
    return f"source::{source_id}::{(version or '')[:16] or '0' * 16}"


class SubscribeSupervisor:
    def __init__(
        self, pool: Any, *,
        resolve: Callable[[str], Awaitable[Any]] = resolve_source_deps,
        lease_factory: Callable[[Any, str], Any] | None = None,
        connector_factory: Callable[..., SubscribeConnector] | None = None,
        interval_s: float | None = None,
    ) -> None:
        self._pool = pool
        self._resolve = resolve
        self._lease_factory = lease_factory or (lambda pool, sid: PgAdvisoryLease(pool, sid))
        self._connector_factory = connector_factory or SubscribeConnector
        try:
            self._interval = float(interval_s or os.environ.get(_INTERVAL_ENV, "") or 60.0)
        except ValueError:
            self._interval = 60.0
        self.connectors: dict[str, tuple[str, SubscribeConnector]] = {}   # source_id -> (actor_id, c)
        self._task: asyncio.Task | None = None

    async def _heads(self) -> dict[str, str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(_HEADS_SQL)
        return {r["descriptor_id"]: _actor_id(r["descriptor_id"], r["version"]) for r in rows}

    async def tick(self) -> dict[str, list[str]]:
        """One reconcile pass. Returns what it did (for logs and tests)."""
        done: dict[str, list[str]] = {"started": [], "restarted": [], "stopped": []}
        heads = await self._heads()
        for source_id in list(self.connectors):
            actor_id, conn = self.connectors[source_id]
            if heads.get(source_id) != actor_id:
                await conn.stop()
                del self.connectors[source_id]
                done["stopped"].append(source_id)
        for source_id, actor_id in sorted(heads.items()):
            held = self.connectors.get(source_id)
            if held is not None and held[1].running:
                continue
            restarted = held is not None
            connector = await self._build(source_id, actor_id)
            if connector is None:
                continue
            self.connectors[source_id] = (actor_id, connector.start())
            done["restarted" if restarted else "started"].append(source_id)
        if any(done.values()):
            logger.info("stream.supervisor.tick %s", {k: v for k, v in done.items() if v})
        return done

    async def _build(self, source_id: str, actor_id: str) -> SubscribeConnector | None:
        try:
            sd = await self._resolve(actor_id)
        except Exception as exc:
            logger.warning("stream.supervisor.resolve_failed source=%s err=%s", source_id, exc)
            return None
        if sd is None:
            logger.warning("stream.supervisor.no_deps source=%s actor_id=%s", source_id, actor_id)
            return None
        try:
            core = StreamCore(actor_id, sd)
        except Exception as exc:  # not a subscribed head after all, a refused media policy, …
            logger.error("stream.supervisor.refused source=%s err=%s", source_id, exc)
            return None
        return self._connector_factory(core, lease=self._lease_factory(self._pool, source_id))

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a registry read failure: try again next tick
                logger.warning("stream.supervisor.tick_failed err=%s", exc)
            await asyncio.sleep(self._interval)

    async def start(self) -> "SubscribeSupervisor":
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run_forever(), name="legba-subscribe-supervisor")
        return self

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None
        conns = [c for _, c in self.connectors.values()]
        self.connectors.clear()
        await asyncio.gather(*(c.stop() for c in conns), return_exceptions=True)


async def start_subscribe_supervisor_if_enabled(pool: Any) -> SubscribeSupervisor | None:
    if not subscribe_supervisor_enabled():
        logger.info("stream.supervisor.off (%s unset)", SUPERVISOR_ENV)
        return None
    sup = await SubscribeSupervisor(pool).start()
    logger.info("stream.supervisor.started")
    return sup
