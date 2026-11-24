# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The held-family feed on a loop (data-model lane 10, DM-C) — OFF by default.

With ``LEGBA_HELD_RECORD_SHADOW`` naming desks, every
``LEGBA_HELD_RECORD_INTERVAL_S`` (600) the loop writes at most
``LEGBA_HELD_RECORD_PASS_LIMIT`` (20) pending desk shadow records into the
held family (:func:`legba.data.held_record_feed.feed_pending`). Unset: nothing
starts. A pass that fails is logged and the loop keeps going; nothing it does
reaches a desk run, a finding or a reader surface.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = ["HeldRecordLoop", "start_held_record_if_enabled"]


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


class HeldRecordLoop:
    def __init__(self, pool: Any, *, interval_s: Optional[float] = None) -> None:
        self._pool = pool
        self._interval = float(interval_s if interval_s is not None
                               else _num("LEGBA_HELD_RECORD_INTERVAL_S", 600.0))
        self._task: Optional[asyncio.Task] = None
        self._stopped = False

    async def run_once(self) -> Any:
        from ..data.held_record_feed import feed_pending

        rep = await feed_pending(self._pool, limit=int(_num("LEGBA_HELD_RECORD_PASS_LIMIT", 20)))
        if rep.read:
            logger.info("held_record.pass read=%d written=%d already=%d errors=%d assertions=%d versions=%d",
                        rep.read, rep.written, rep.already, rep.errors, rep.counts.get("assertions", 0),
                        rep.counts.get("versions", 0))
        return rep

    async def run_forever(self) -> None:
        while not self._stopped:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a DB blip: log, keep going
                logger.warning("held_record.pass_error err=%s", type(exc).__name__)
            await asyncio.sleep(self._interval)

    async def start(self) -> "HeldRecordLoop":
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(self.run_forever(), name="legba-held-record")
        return self

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


async def start_held_record_if_enabled(pool: Any) -> Optional[HeldRecordLoop]:
    from ..data.held_record_feed import FEED_ENV, feed_analysts

    analysts = feed_analysts()
    if not analysts:
        logger.info("held_record.disabled %s is unset", FEED_ENV)
        return None
    loop = await HeldRecordLoop(pool).start()
    logger.info("held_record.started analysts=%s interval_s=%s", sorted(analysts), loop._interval)
    return loop
