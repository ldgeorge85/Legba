# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-stage enrichment latency counters (W5 SRC-4), in a stdlib-only leaf.

Where does a signal's wall time go between the poll and the write? The
enrichment chain a source declares (``pipeline.enrichment``: language_detect /
geocode / ner_multilingual / classify / ...) is built once per source-deps
resolution by ``dapr_host._source_enrichment_factory``; each handler is wrapped
here by :func:`timed_stage` so every ``transform`` call is timed on the
monotonic clock. Three things hold the numbers:

  * a per-process ring of the last :data:`RING_SIZE` call durations per stage
    kind (:func:`ring_ms`) -- the in-process distribution, lost on restart;
  * a per-source, per-poll accumulator ``{kind: {calls, total_ms, max_ms,
    errors}}`` that SourceCore drains once per poll (:func:`cursor_fields`)
    into the source's ``cursor`` row in ``actor_filter_state`` -- the same row
    the SRC-3 keyword counters live on:

      ``enrichment_last_poll``     the stage summary of the newest poll that
                                    ENRICHED at least one signal
      ``enrichment_polled_at``     when that poll ran (the figure's as-of)

    A poll that enriched nothing (an empty or all-gated poll) carries the prior
    summary forward unchanged -- overwriting it would leave the key absent on
    most polls. A source whose chain has never run has neither key: absent
    means "not measured", never 0;
  * an INFO line per poll for any stage whose total exceeded
    :data:`SLOW_STAGE_MS`: ``enrichment.slow source_id=... kind=... total_ms=...``.

An error is a handler ``transform`` that raised (including a cancellation by
the enrich timeout); it is counted and re-raised, so behaviour is unchanged.
:func:`fetch_enrichment_last_poll` is the fleet read the source-health and
``/v3/source-quality`` surfaces share.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

#: Last-N call durations kept per stage kind, per process.
RING_SIZE = 500
#: A stage whose per-poll total exceeds this logs ``enrichment.slow``.
SLOW_STAGE_MS = 10_000.0

#: ``actor_filter_state.filter_id`` of a source's SourceCore state is
#: ``source:<source_id>`` (``source_actor.SourceCore``).
_FILTER_PREFIX = "source:"

_ring: dict[str, deque[float]] = {}
_poll: dict[str, dict[str, dict[str, float]]] = {}


def _record(source_id: str | None, kind: str, ms: float, *, error: bool) -> None:
    _ring.setdefault(kind, deque(maxlen=RING_SIZE)).append(ms)
    if source_id is None:
        return
    slot = _poll.setdefault(source_id, {}).setdefault(
        kind, {"calls": 0, "total_ms": 0.0, "max_ms": 0.0, "errors": 0},
    )
    slot["calls"] += 1
    slot["total_ms"] += ms
    slot["max_ms"] = max(slot["max_ms"], ms)
    if error:
        slot["errors"] += 1


class _TimedHandler:
    """A filter handler whose ``transform`` is timed; every other attribute
    (``health_check``, ``gate``, ...) is the wrapped handler's own."""

    def __init__(self, kind: str, handler: Any, source_id: str | None) -> None:
        self._kind = kind
        self._handler = handler
        self._source_id = source_id

    async def transform(self, signal: Any, ctx: Any) -> Any:
        t0 = time.monotonic()
        failed = False
        try:
            return await self._handler.transform(signal, ctx)
        except BaseException:
            failed = True
            raise
        finally:
            _record(
                self._source_id, self._kind,
                (time.monotonic() - t0) * 1000.0, error=failed,
            )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handler, name)


def timed_stage(kind: str, handler: Any, source_id: str | None = None) -> Any:
    """Wrap ``handler`` so each ``transform`` call is timed under ``kind``
    (and, with ``source_id``, accumulated for that source's next cursor)."""
    return _TimedHandler(kind, handler, source_id)


def ring_ms(kind: str) -> list[float]:
    """The in-process ring of the last call durations (ms) for ``kind``."""
    return list(_ring.get(kind, ()))


def _drain(source_id: str) -> dict[str, dict[str, float | int]]:
    got = _poll.pop(source_id, None) or {}
    out: dict[str, dict[str, float | int]] = {}
    for kind, s in got.items():
        out[kind] = {
            "calls": int(s["calls"]),
            "total_ms": round(s["total_ms"], 1),
            "max_ms": round(s["max_ms"], 1),
            "errors": int(s["errors"]),
        }
        if s["total_ms"] > SLOW_STAGE_MS:
            logger.info(
                "enrichment.slow source_id=%s kind=%s total_ms=%.0f",
                source_id, kind, s["total_ms"],
            )
    return out


def cursor_fields(source_id: str, prior_cursor: Any) -> dict[str, Any]:
    """Drain this poll's stage timings into cursor fields.

    ``{}`` when the source has never been measured. A poll that enriched
    nothing carries the prior summary (and its as-of) forward unchanged.
    """
    polled = _drain(source_id)
    if polled:
        return {
            "enrichment_last_poll": polled,
            "enrichment_polled_at": datetime.now(timezone.utc).isoformat(),
        }
    prior = prior_cursor if isinstance(prior_cursor, dict) else {}
    carried = prior.get("enrichment_last_poll")
    if not isinstance(carried, dict) or not carried:
        return {}
    out: dict[str, Any] = {"enrichment_last_poll": carried}
    at = prior.get("enrichment_polled_at")
    if isinstance(at, str) and at:
        out["enrichment_polled_at"] = at
    return out


_FETCH_SQL = """
SELECT DISTINCT ON (filter_id)
       filter_id,
       value -> 'enrichment_last_poll' AS stages,
       value ->> 'enrichment_polled_at' AS polled_at
  FROM actor_filter_state
 WHERE state_key = 'cursor'
   AND filter_id = ANY($1::text[])
   AND value ? 'enrichment_last_poll'
 ORDER BY filter_id, updated_at DESC
"""


def _as_dict(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


async def fetch_enrichment_last_poll(
    pool: Any, source_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    """``{source_id: {"stages": {kind: {calls, total_ms, max_ms, errors}},
    "polled_at": iso|None}}`` for the sources whose cursor row carries a stage
    summary; a source absent from the result is NOT MEASURED. Raises on a DB
    error -- the caller decides what an unreadable ledger means."""
    ids = list(dict.fromkeys(source_ids))
    if not ids:
        return {}
    async with pool.acquire() as conn:
        records = await conn.fetch(_FETCH_SQL, [_FILTER_PREFIX + i for i in ids])
    out: dict[str, dict[str, Any]] = {}
    for r in records:
        stages = _as_dict(r["stages"])
        if not stages:
            continue
        out[str(r["filter_id"])[len(_FILTER_PREFIX):]] = {
            "stages": stages,
            "polled_at": r["polled_at"],
        }
    return out


__all__ = [
    "RING_SIZE", "SLOW_STAGE_MS", "cursor_fields", "fetch_enrichment_last_poll",
    "ring_ms", "timed_stage",
]
