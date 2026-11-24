# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Event-loop lag monitor — names whatever holds the runtime's loop.

N-V (2026-11-13): the runtime froze for ~116 s every 20 minutes for weeks —
no log line, no poll, no actor call, ``/healthz`` unanswered and the container
marked unhealthy — because one deterministic analyst (``event_reconciler``)
ran a quadratic pair walk as plain Python on the asyncio loop. Nothing in the
process said so; the cause was found by lining up gaps in the log timestamps
with analyst run times. This monitor makes the next offender name itself.

Two cheap halves:

* a heartbeat coroutine on the loop stamps ``time.monotonic()`` every
  ``tick`` seconds (one ``asyncio.sleep`` wake-up per tick);
* a daemon watchdog THREAD checks the stamp every ``tick``. When the stamp is
  older than ``threshold`` the loop is blocked; while it is still blocked the
  thread reads the loop's current task (``asyncio.current_task(loop)``) and
  the loop thread's innermost frames (``sys._current_frames``) and logs ONE
  ``loop_lag.blocked`` warning naming them. When the heartbeat resumes it logs
  ``loop_lag.recovered`` with the measured block length.

The thread does work only when a stall is in progress, so the steady-state
cost is two timer wake-ups per tick. ``LEGBA_LOOP_LAG_WARN_SECONDS`` sets the
threshold (default 2 s); ``0`` or less disables the monitor.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from typing import Any, Callable

logger = logging.getLogger(__name__)

LOOP_LAG_WARN_ENV = "LEGBA_LOOP_LAG_WARN_SECONDS"
DEFAULT_WARN_SECONDS = 2.0
DEFAULT_TICK_SECONDS = 0.25
_STACK_FRAMES = 6
#: N-W — how many of the innermost OWN (``legba``) frames to append when the
#: innermost ``_STACK_FRAMES`` are all library code (croniter, the stdlib
#: resolver, ...): the first live day logged ``croniter.py`` six deep with no
#: hint of which analyst had called it.
_APP_FRAMES = 4
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _frame_label(f: traceback.FrameSummary) -> str:
    """``pkg-relative/path.py:line:func`` for our frames, basename otherwise."""
    path = os.path.abspath(f.filename)
    if path.startswith(_PKG_ROOT + os.sep):
        name = os.path.relpath(path, _PKG_ROOT)
    else:
        name = os.path.basename(f.filename)
    return f"{name}:{f.lineno}:{f.name}"


def describe_stack(frame: Any) -> str:
    """The innermost frames of a blocked loop thread, innermost first, plus —
    when those are all library frames — the innermost application frames
    below them, after ``| app:``, so every report names OUR caller."""
    return describe_frames(traceback.extract_stack(frame))


def describe_frames(stack: list[traceback.FrameSummary]) -> str:
    """:func:`describe_stack` over an already-extracted stack (outermost
    first)."""
    inner = stack[-_STACK_FRAMES:]
    where = " <- ".join(_frame_label(f) for f in reversed(inner))
    own = [
        f for f in stack[:-_STACK_FRAMES]
        if os.path.abspath(f.filename).startswith(_PKG_ROOT + os.sep)
    ]
    if own and not any(
        os.path.abspath(f.filename).startswith(_PKG_ROOT + os.sep) for f in inner
    ):
        where += " | app: " + " <- ".join(
            _frame_label(f) for f in reversed(own[-_APP_FRAMES:])
        )
    return where


def warn_seconds_from_env() -> float:
    """The configured threshold; ``<= 0`` means disabled."""
    raw = os.getenv(LOOP_LAG_WARN_ENV, "").strip()
    if not raw:
        return DEFAULT_WARN_SECONDS
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "loop_lag.bad_threshold %s=%r; using %s",
            LOOP_LAG_WARN_ENV, raw, DEFAULT_WARN_SECONDS,
        )
        return DEFAULT_WARN_SECONDS


class LoopLagMonitor:
    """Heartbeat on the loop + watchdog thread that reports blocking."""

    def __init__(
        self,
        *,
        threshold_seconds: float = DEFAULT_WARN_SECONDS,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        log: logging.Logger | None = None,
    ) -> None:
        self.threshold = float(threshold_seconds)
        self.tick = max(0.01, min(float(tick_seconds), self.threshold / 2))
        self._clock = clock
        self._log = log or logger
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_id: int | None = None
        self._beat_task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_beat = 0.0
        self._stall_since: float | None = None
        #: Stalls reported so far, and what each named (for tests/ops).
        self.stalls: list[dict[str, Any]] = []
        self.max_lag_seconds = 0.0

    # -- loop side --------------------------------------------------------
    async def start(self) -> "LoopLagMonitor":
        """Start the heartbeat and the watchdog; call from the loop."""
        self._loop = asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()
        self._last_beat = self._clock()
        self._beat_task = self._loop.create_task(
            self._heartbeat(), name="loop_lag_monitor.heartbeat"
        )
        self._thread = threading.Thread(
            target=self._watch, name="loop-lag-watchdog", daemon=True
        )
        self._thread.start()
        return self

    async def stop(self) -> None:
        """Stop both halves; safe to call twice."""
        self._stop.set()
        task, self._beat_task = self._beat_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            await asyncio.to_thread(thread.join, 5.0)

    async def _heartbeat(self) -> None:
        while True:
            self._last_beat = self._clock()
            await asyncio.sleep(self.tick)

    # -- watchdog thread --------------------------------------------------
    def _watch(self) -> None:
        while not self._stop.wait(self.tick):
            self.check()

    def check(self) -> None:
        """One watchdog look (the thread calls this every tick)."""
        beat = self._last_beat
        lag = self._clock() - beat
        if lag > self.max_lag_seconds:
            self.max_lag_seconds = lag
        if lag > self.threshold:
            if self._stall_since != beat:
                self._stall_since = beat
                self._report_blocked(lag)
            return
        if self._stall_since is not None:
            blocked_for = beat - self._stall_since
            self._stall_since = None
            if self.stalls:
                self.stalls[-1]["blocked_seconds"] = round(blocked_for, 3)
            self._log.warning(
                "loop_lag.recovered blocked_for=%.1fs task=%s",
                blocked_for, self.stalls[-1]["task"] if self.stalls else "?",
            )

    def _report_blocked(self, lag: float) -> None:
        task_name = "?"
        coro = "?"
        if self._loop is not None:
            try:
                task = asyncio.current_task(self._loop)
            except Exception:                       # pragma: no cover
                task = None
            if task is not None:
                task_name = task.get_name()
                coro = getattr(
                    task.get_coro(), "__qualname__", repr(task.get_coro())
                )
        where = ""
        frame = sys._current_frames().get(self._loop_thread_id or -1)
        if frame is not None:
            where = describe_stack(frame)
        self.stalls.append(
            {"task": task_name, "coro": coro, "where": where,
             "lag_seconds": round(lag, 3)}
        )
        self._log.warning(
            "loop_lag.blocked lag=%.1fs threshold=%.1fs task=%s coro=%s "
            "where=%s — the event loop is held by synchronous work; every "
            "actor turn, poll and /healthz waits until it returns",
            lag, self.threshold, task_name, coro, where or "?",
        )


async def start_loop_lag_monitor_from_env() -> LoopLagMonitor | None:
    """Start a monitor at the configured threshold, or ``None`` if disabled."""
    threshold = warn_seconds_from_env()
    if threshold <= 0:
        logger.info("loop_lag.disabled %s=%s", LOOP_LAG_WARN_ENV, threshold)
        return None
    monitor = await LoopLagMonitor(threshold_seconds=threshold).start()
    logger.info("loop_lag.started threshold=%.1fs tick=%.2fs",
                monitor.threshold, monitor.tick)
    return monitor


__all__ = [
    "DEFAULT_WARN_SECONDS",
    "LOOP_LAG_WARN_ENV",
    "LoopLagMonitor",
    "describe_frames",
    "describe_stack",
    "start_loop_lag_monitor_from_env",
    "warn_seconds_from_env",
]
