# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ONE process-wide concurrency bound on the core-plane model.

Why
---
The core model is served from a self-run gateway that needs the number of
concurrent requests kept modest. Before this module
the only client-side bound was the component's ``max_concurrent``, held as an
asyncio semaphore on each HANDLER INSTANCE and each event loop
(``vllm.py``'s ``_concurrency_sems``). That is not a process bound: the
runtime's cached handler, a consult per-request plane resolver, the evidence
drift loop and every deep-consult activation (one fresh handler per workflow,
one ``asyncio.run`` loop per activity, ``deep_consult_workflow.py``) each held
their own 16 slots.

What
----
:class:`CallGate` is one bound per process for the core component, shared by
every handler instance and every event loop in the process (a
``threading.Lock`` guards the state; a waiter on another loop is woken with
``call_soon_threadsafe``). It is acquired around the NETWORK HOP only
(``base.LLMProviderHandler.chat_complete`` wraps ``_call_chat``), so a held
slot is always a request in flight against the gateway.

* **Never drops a call and never caps a token.** A caller waits; nothing is
  refused, timed out or shortened here. A caller's own deadline (the consult
  round deadline) may cancel its wait, which simply leaves the queue.
* **FIFO-fair** within each class: waiters are granted strictly in arrival
  order.
* **Interactive calls are not starved by scheduled ones.** Two classes:
  ``interactive`` (a person is waiting: Consult turns, deep consult) and
  ``scheduled`` (everything else, the default). Scheduled calls may hold at
  most ``limit - reserved`` slots, so ``reserved`` slots are always free for
  interactive calls; when a slot frees, a queued interactive call is granted
  before a queued scheduled one. Interactive calls may use any slot.
* **Visible.** Every call that waited carries ``gate_wait_ms`` on its
  ``analyst_traces.llm_calls`` receipt (sparse: absent when it did not wait);
  a wait of :data:`LOG_WAIT_S` or more logs ``llm.gate.waited`` with the queue
  depth; :func:`gate_snapshots` (served on the runtime's ``/healthz``) reports
  in-flight, queued, peaks and wait totals.

Configuration (environment, read when the gate is first built in a process):

* ``LEGBA_CORE_LLM_COMPONENT_ID`` — the component the bound applies to
  (default ``llm.primary.openai_compat``). Other components keep their own
  per-instance ``max_concurrent`` exactly as before.
* ``LEGBA_CORE_LLM_MAX_CONCURRENT`` — the bound (default
  :data:`DEFAULT_LIMIT` = 12). On the core component it supersedes the
  component row's ``max_concurrent``.
* ``LEGBA_CORE_LLM_INTERACTIVE_RESERVED`` — slots scheduled calls may not
  take (default :data:`DEFAULT_RESERVED` = 2; clamped to ``limit - 1``).

The default, from the receipts (read-only, 7 days to 2026-11-14): 31,769
core-plane calls inside runs; peak 7 in flight at once, p99 5, p50 1. A
scheduled cap of 10 (12 - 2) is above that peak, so today's schedule never
waits, two slots stay free for a person, and the process total is below the
old per-instance 16 that several instances could each fill.

Stdlib-only leaf: the registry's slim image and the workflow worker import it
alike.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterator

logger = logging.getLogger(__name__)

INTERACTIVE = "interactive"
SCHEDULED = "scheduled"
PRIORITIES: tuple[str, str] = (INTERACTIVE, SCHEDULED)

COMPONENT_ENV = "LEGBA_CORE_LLM_COMPONENT_ID"
LIMIT_ENV = "LEGBA_CORE_LLM_MAX_CONCURRENT"
RESERVED_ENV = "LEGBA_CORE_LLM_INTERACTIVE_RESERVED"

DEFAULT_COMPONENT_ID = "llm.primary.openai_compat"
DEFAULT_LIMIT = 12
DEFAULT_RESERVED = 2

#: A wait at least this long is logged (``llm.gate.waited``).
LOG_WAIT_S = 1.0

_priority: ContextVar[str] = ContextVar("legba_llm_call_priority", default=SCHEDULED)


def current_priority() -> str:
    """The priority class of calls made from the current context."""
    return _priority.get()


@contextlib.contextmanager
def interactive_calls() -> Iterator[None]:
    """Mark every model call made inside the block as ``interactive``."""
    token = _priority.set(INTERACTIVE)
    try:
        yield
    finally:
        _priority.reset(token)


@dataclass
class _Waiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[None]
    priority: str
    enqueued: float
    granted: bool = False


@dataclass
class SlotTicket:
    """What one acquisition cost: the class it ran in and how long it queued."""

    priority: str
    wait_s: float = 0.0

    @property
    def wait_ms(self) -> int:
        return int(self.wait_s * 1000)


@dataclass
class _Stats:
    granted: int = 0
    waited: int = 0
    wait_total_s: float = 0.0
    wait_max_s: float = 0.0
    peak_in_flight: int = 0
    peak_queued: int = 0
    by_priority: dict[str, int] = field(
        default_factory=lambda: {p: 0 for p in PRIORITIES},
    )


def _wake(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)


class CallGate:
    """A process-wide, cross-loop, two-class FIFO bound on in-flight calls."""

    def __init__(self, name: str, *, limit: int, reserved: int) -> None:
        if limit < 1:
            raise ValueError(f"call gate {name!r}: limit must be >= 1, got {limit}")
        self.name = name
        self.limit = limit
        self.reserved = max(0, min(reserved, limit - 1))
        self._lock = threading.Lock()
        self._in_flight = {p: 0 for p in PRIORITIES}
        self._queues: dict[str, deque[_Waiter]] = {p: deque() for p in PRIORITIES}
        self._stats = _Stats()

    # ---- admission (all under self._lock) --------------------------------

    def _total_in_flight(self) -> int:
        return self._in_flight[INTERACTIVE] + self._in_flight[SCHEDULED]

    def _queued(self) -> int:
        return len(self._queues[INTERACTIVE]) + len(self._queues[SCHEDULED])

    def _admissible(self, priority: str) -> bool:
        if self._total_in_flight() >= self.limit:
            return False
        if priority == SCHEDULED:
            return self._in_flight[SCHEDULED] < self.limit - self.reserved
        return True

    def _take(self, priority: str) -> None:
        self._in_flight[priority] += 1
        self._stats.granted += 1
        self._stats.by_priority[priority] += 1
        self._stats.peak_in_flight = max(self._stats.peak_in_flight, self._total_in_flight())

    def _dispatch(self) -> None:
        """Grant queued waiters while slots allow: interactive first, FIFO."""
        while True:
            for priority in PRIORITIES:
                queue = self._queues[priority]
                if queue and self._admissible(priority):
                    waiter = queue.popleft()
                    self._take(priority)
                    waiter.granted = True
                    try:
                        waiter.loop.call_soon_threadsafe(_wake, waiter.future)
                    except RuntimeError:
                        # The waiter's loop is closed (an activity's
                        # ``asyncio.run`` ended): nobody will use the slot.
                        self._in_flight[priority] -= 1
                    break
            else:
                return

    # ---- public surface --------------------------------------------------

    async def acquire(self, priority: str | None = None) -> SlotTicket:
        """Wait for a slot (never refused, never timed out here)."""
        prio = priority if priority in PRIORITIES else current_priority()
        loop = asyncio.get_running_loop()
        with self._lock:
            if not self._queues[prio] and self._admissible(prio):
                self._take(prio)
                return SlotTicket(priority=prio)
            waiter = _Waiter(loop, loop.create_future(), prio, time.monotonic())
            self._queues[prio].append(waiter)
            self._stats.peak_queued = max(self._stats.peak_queued, self._queued())
            depth = self._queued()
        try:
            await waiter.future
        except BaseException:
            with self._lock:
                if waiter.granted:
                    # Granted while being cancelled: hand the slot on.
                    self._in_flight[prio] -= 1
                    self._dispatch()
                else:
                    with contextlib.suppress(ValueError):
                        self._queues[prio].remove(waiter)
            raise
        wait_s = time.monotonic() - waiter.enqueued
        with self._lock:
            self._stats.waited += 1
            self._stats.wait_total_s += wait_s
            self._stats.wait_max_s = max(self._stats.wait_max_s, wait_s)
        if wait_s >= LOG_WAIT_S:
            logger.info(
                "llm.gate.waited gate=%s priority=%s wait_ms=%d queued_at_entry=%d "
                "limit=%d reserved=%d",
                self.name, prio, int(wait_s * 1000), depth, self.limit, self.reserved,
            )
        return SlotTicket(priority=prio, wait_s=wait_s)

    def release(self, priority: str) -> None:
        with self._lock:
            if self._in_flight[priority] > 0:
                self._in_flight[priority] -= 1
            self._dispatch()

    @contextlib.asynccontextmanager
    async def slot(self, priority: str | None = None) -> AsyncIterator[SlotTicket]:
        ticket = await self.acquire(priority)
        try:
            yield ticket
        finally:
            self.release(ticket.priority)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            s = self._stats
            return {
                "gate": self.name,
                "limit": self.limit,
                "interactive_reserved": self.reserved,
                "in_flight": dict(self._in_flight),
                "queued": {p: len(q) for p, q in self._queues.items()},
                "granted": s.granted,
                "granted_by_priority": dict(s.by_priority),
                "waited": s.waited,
                "wait_total_ms": int(s.wait_total_s * 1000),
                "wait_max_ms": int(s.wait_max_s * 1000),
                "peak_in_flight": s.peak_in_flight,
                "peak_queued": s.peak_queued,
            }


# ---------------------------------------------------------------------------
# The process registry: one gate per gated component id.
# ---------------------------------------------------------------------------

_GATES: dict[str, CallGate] = {}
_GATES_LOCK = threading.Lock()


def _env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = minimum - 1
    if value < minimum:
        logger.warning(
            "llm.gate.bad_env %s=%r is not an integer >= %d; using %d",
            name, raw, minimum, default,
        )
        return default
    return value


def core_component_id() -> str:
    return os.getenv(COMPONENT_ENV, "").strip() or DEFAULT_COMPONENT_ID


def gate_for_component(component_id: str | None) -> CallGate | None:
    """The process gate for ``component_id``, or ``None`` when it is not gated."""
    if not component_id or component_id != core_component_id():
        return None
    gate = _GATES.get(component_id)
    if gate is not None:
        return gate
    with _GATES_LOCK:
        gate = _GATES.get(component_id)
        if gate is None:
            limit = _env_int(LIMIT_ENV, DEFAULT_LIMIT, minimum=1)
            reserved = _env_int(RESERVED_ENV, DEFAULT_RESERVED, minimum=0)
            gate = CallGate(component_id, limit=limit, reserved=reserved)
            _GATES[component_id] = gate
            logger.info(
                "llm.gate.built gate=%s limit=%d interactive_reserved=%d",
                component_id, gate.limit, gate.reserved,
            )
        return gate


def gate_snapshots() -> list[dict[str, Any]]:
    """Every gate built in this process (empty until the first gated call)."""
    with _GATES_LOCK:
        gates = list(_GATES.values())
    return [g.snapshot() for g in gates]


def clear_gates() -> None:
    """Forget every gate (tests; a new env takes effect on the next call)."""
    with _GATES_LOCK:
        _GATES.clear()


__all__ = [
    "CallGate",
    "DEFAULT_COMPONENT_ID",
    "DEFAULT_LIMIT",
    "DEFAULT_RESERVED",
    "INTERACTIVE",
    "SCHEDULED",
    "SlotTicket",
    "clear_gates",
    "core_component_id",
    "current_priority",
    "gate_for_component",
    "gate_snapshots",
    "interactive_calls",
]
