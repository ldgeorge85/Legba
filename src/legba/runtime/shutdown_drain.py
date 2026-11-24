# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-F84 — a bounded drain of the actor host at SIGTERM.

THE DEFECT. Every rolling deploy recreates the runtime. An analyst run in flight
at that moment has already WRITTEN its finding, but the verify leg that writes
its critique never runs, so the finding stands published unverified.

WHY NOTHING DRAINED. ``dapr_host.main`` serves the actor surface with
``uvicorn.Server.serve``, and uvicorn (>= 0.29) captures SIGTERM itself to shut
down gracefully: close the listener (daprd can deliver no NEW actor call — no
reminder, fan-out ``run`` or heal — to this process), wait for every in-flight
request (each one an actor turn: daprd calls ``/actors/<Type>/<id>/method/…``
and the SDK awaits the method inside it), then run the lifespan shutdown. But
``main`` ALSO registered ``loop.add_signal_handler(SIGTERM, loop.stop)``, and
uvicorn's ``signal.signal`` override does not silence it: Python's C-level
handler still writes the signal to asyncio's wakeup fd, so both fire, and
``loop.stop()`` abandons every in-flight turn at once ("Event loop stopped
before Future completed"). Docker's 10 s stop timeout was never even reached.

THE STEP (opt-in; default byte-identical).

  * ``LEGBA_RUNTIME_DRAIN_SECONDS=N`` (1-60, in ``.env``): the first SIGTERM /
    SIGINT does NOT stop the loop. uvicorn's graceful shutdown runs, bounded by
    ``timeout_graceful_shutdown=N`` (past it the remaining turns are cancelled
    and the lifespan shutdown — NATS, the leader lease, handler teardown —
    still runs); a backstop stops the loop at ``N +`` :data:`BACKSTOP_MARGIN_S`
    whatever happens. A SECOND signal stops it at once, as before.
  * ``scripts/rolling_deploy.sh --drain-seconds M`` passes ``--timeout M`` to the
    runtime recreate, so docker waits M seconds after SIGTERM before SIGKILL
    (the compose default is 10 s). Use ``M >= N + BACKSTOP_MARGIN_S``.
  * Unset / 0: ``loop.stop()`` on the first signal, exactly as before.

A run that outlives the bound is cut as before; a finding it left without a
critique is what the ``verify_backfill`` sweep repays.
"""

from __future__ import annotations

import logging
import os
import signal as signalmod
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

#: The env knob bounding the drain, in seconds.
DRAIN_SECONDS_ENV = "LEGBA_RUNTIME_DRAIN_SECONDS"
#: The ceiling on any drain — a deploy never waits longer than this per host.
MAX_DRAIN_SECONDS = 60
#: The backstop fires this long after the drain bound: uvicorn's cancel of the
#: remaining turns and the lifespan shutdown get this much room.
BACKSTOP_MARGIN_S = 10


def runtime_drain_seconds() -> int | None:
    """The drain bound for the actor host, or ``None`` for no drain.

    ``None`` when unset, empty, ``<= 0`` or unparseable (the last with a
    warning). Values above :data:`MAX_DRAIN_SECONDS` are clamped to it.
    """
    raw = (os.getenv(DRAIN_SECONDS_ENV) or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "shutdown_drain.bad_value %s=%r — not an integer; no drain (the first "
            "signal stops the host at once)", DRAIN_SECONDS_ENV, raw,
        )
        return None
    if value <= 0:
        return None
    return min(value, MAX_DRAIN_SECONDS)


def install_shutdown_signal_handlers(
    loop: Any,
    *,
    drain_seconds: int | None,
    signals: Iterable[int] = (signalmod.SIGINT, signalmod.SIGTERM),
) -> Callable[[], None]:
    """Register the actor host's loop-level shutdown handler; returns it.

    ``drain_seconds=None``: the first signal stops the loop (the old handler).
    Otherwise the first signal only arms the backstop and leaves the graceful
    shutdown to uvicorn; a repeat signal stops the loop at once.
    """
    received = {"n": 0}

    def _handler(*_: Any) -> None:
        received["n"] += 1
        if drain_seconds is None or received["n"] > 1:
            logger.info("legba-runtime-dapr received shutdown signal")
            loop.call_soon_threadsafe(loop.stop)
            return
        logger.info(
            "legba-runtime-dapr received shutdown signal — draining in-flight actor "
            "turns for up to %ss (backstop %ss; a second signal stops at once)",
            drain_seconds, drain_seconds + BACKSTOP_MARGIN_S,
        )
        loop.call_later(drain_seconds + BACKSTOP_MARGIN_S, loop.stop)

    for sig in signals:
        try:
            loop.add_signal_handler(sig, _handler)
        except NotImplementedError:  # pragma: no cover — Windows
            pass
    return _handler


__all__ = [
    "BACKSTOP_MARGIN_S",
    "DRAIN_SECONDS_ENV",
    "MAX_DRAIN_SECONDS",
    "install_shutdown_signal_handlers",
    "runtime_drain_seconds",
]
