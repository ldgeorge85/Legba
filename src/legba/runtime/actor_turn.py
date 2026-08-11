# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn budgets for the actor plane — the S-6 answer to the zombie turn.

WHAT WENT WRONG ON 2026-08-01, in one paragraph. A strict-mode parse bug made
``proxy.activate()`` **hang** rather than fail. ``ENSURE_ACTIVE`` — the
reconciler's durability heal — fires against every active analyst and source on
every periodic resync, so each hung activate consumed the full 90 s
``run_once`` bound; the reconcile main loop is strictly serial, so the queue
crawled at one descriptor per 90 seconds (visible in the registry access log as
a single, perfectly-spaced GET every 90 s). And Dapr actors are **turn-based**
with reentrancy disabled everywhere, so each hung activate occupied its actor's
turn queue *indefinitely*: every subsequent cadence reminder and every
coalesced fire for that actor queued behind a turn that would never complete.
Within one resync cycle the entire plane was turn-poisoned by its own
durability heal. The freeze lasted 35 minutes and ended only because the host
stall watchdog restarted the runtime.

Two things were missing, and this module supplies both. They are a **pair** —
neither alone is sufficient, and the reason is worth stating because it is
counter-intuitive:

1. **A deadline on the reconciler's per-actor heal.** Bounds what ONE wedged
   actor costs the reconcile queue. :func:`heal_timeout_seconds` is deliberately
   far below the 90 s ``run_once`` bound, so a hung heal is a short skip instead
   of eating the whole pass, and the queue keeps draining.

2. **Bounded ops INSIDE the actor turn.** The deadline in (1) cancels the
   caller's coroutine; it does **not** unwedge the actor. The turn is held by
   the actor runtime's own per-id lock in the app process, so from the
   reconciler's side a cancelled activate leaves the turn queue exactly as
   poisoned as before — it just stops paying for it. Only bounding the
   hang-prone I/O *within* the turn (the registry deps refetch, the reminder
   registration, upstream provisioning) makes the turn actually COMPLETE, which
   is what releases the queue behind it.

Plus a third piece that makes the degradation stable rather than merely
survivable: :class:`HealBreaker`. Without it a wedged actor is re-poked on every
resync forever, so the plane pays the deadline over and over for an actor that
is provably not answering. The breaker converts repeated timeouts into
skip-and-retry with a cooloff — the heal is idempotent and re-runs every resync
anyway, so skipping it is free, and the skip is logged so a wedged actor is
loud rather than invisible.

Everything here is env-overridable and fails safe: an unset, malformed or
non-positive value falls back to the default, so a typo can never silently
disable a budget.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Awaitable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Budgets (env-overridable)
# ---------------------------------------------------------------------------

#: Per-proxy-call deadline for a reconcile lifecycle action. Well under
#: ``ReconcileLoop.run_once_timeout`` (90 s) ON PURPOSE: 90 s is the bound on a
#: whole reconcile pass, and on 08-01 a single hung activate consumed all of it,
#: which is what dragged the effective resync period from its configured 5 min
#: out to ~15 min. A wedged actor should cost the queue seconds, not the pass.
HEAL_TIMEOUT_ENV = "LEGBA_RECONCILE_HEAL_TIMEOUT_SECONDS"
HEAL_TIMEOUT_DEFAULT_S = 20

#: Deadline for one hang-prone op inside an actor turn (a registry deps
#: refetch, a reminder registration, an upstream provisioning call). Larger than
#: the heal deadline because the turn may legitimately be doing real work, and
#: because the goal here is not speed — it is that the turn TERMINATES, so the
#: queue behind it drains.
TURN_OP_TIMEOUT_ENV = "LEGBA_ACTOR_TURN_OP_TIMEOUT_SECONDS"
TURN_OP_TIMEOUT_DEFAULT_S = 30

#: Consecutive heal timeouts on one actor before the breaker opens.
HEAL_BREAKER_TRIPS_ENV = "LEGBA_RECONCILE_HEAL_BREAKER_TRIPS"
HEAL_BREAKER_TRIPS_DEFAULT = 3

#: How long an open breaker suppresses heals for that actor before allowing one
#: probe through. Long enough that a wedged fleet stops costing the queue
#: anything; short enough that recovery is automatic rather than operator-gated.
HEAL_BREAKER_COOLOFF_ENV = "LEGBA_RECONCILE_HEAL_BREAKER_COOLOFF_SECONDS"
HEAL_BREAKER_COOLOFF_DEFAULT_S = 600

#: Hard cap on tracked actors, so the breaker can never become the memory leak
#: it exists to prevent (``_ANALYST_DEPS`` is already an unbounded process-local
#: dict — this module does not add a second one).
_MAX_TRACKED = 5_000


def _positive_int_env(name: str, default: int) -> int:
    """Resolve a positive-int env var, falling back on unset/malformed/<=0.

    Mirrors :func:`legba.runtime.source_first_runtime._positive_int_env`
    deliberately — a budget that silently becomes 0 because of a typo is a
    budget that has been disabled without anyone deciding to.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        val = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "actor_turn.bad_env name=%s value=%r — using default %d",
            name, raw, default,
        )
        return default
    if val <= 0:
        logger.warning(
            "actor_turn.non_positive_env name=%s value=%d — using default %d",
            name, val, default,
        )
        return default
    return val


def heal_timeout_seconds() -> int:
    """Per-proxy-call deadline for a reconcile lifecycle action."""
    return _positive_int_env(HEAL_TIMEOUT_ENV, HEAL_TIMEOUT_DEFAULT_S)


def turn_op_timeout_seconds() -> int:
    """Deadline for one hang-prone op executed inside an actor turn."""
    return _positive_int_env(TURN_OP_TIMEOUT_ENV, TURN_OP_TIMEOUT_DEFAULT_S)


def heal_breaker_trips() -> int:
    """Consecutive heal timeouts before the breaker opens for that actor."""
    return _positive_int_env(HEAL_BREAKER_TRIPS_ENV, HEAL_BREAKER_TRIPS_DEFAULT)


def heal_breaker_cooloff_seconds() -> int:
    """How long an open breaker suppresses heals before one probe is allowed."""
    return _positive_int_env(
        HEAL_BREAKER_COOLOFF_ENV, HEAL_BREAKER_COOLOFF_DEFAULT_S
    )


# ---------------------------------------------------------------------------
# Bounded ops
# ---------------------------------------------------------------------------


class TurnBudgetExceeded(TimeoutError):
    """An op inside an actor turn (or a reconcile heal) blew its deadline.

    Subclasses :exc:`TimeoutError` so callers that already treat a timeout as a
    skip keep working unchanged, while callers that want to distinguish "this
    was OUR budget, not the network's" can catch this specifically. Carries the
    op name and actor id so the log line at the catch site can name the actor
    without the caller re-deriving it.
    """

    def __init__(self, op: str, actor_id: str, timeout_s: float) -> None:
        super().__init__(
            f"{op} on {actor_id} exceeded its {timeout_s:.0f}s turn budget"
        )
        self.op = op
        self.actor_id = actor_id
        self.timeout_s = timeout_s


async def bounded_turn_op(
    awaitable: Awaitable[Any],
    *,
    op: str,
    actor_id: str,
    timeout: float | None = None,
) -> Any:
    """Await ``awaitable`` under a turn deadline; raise on overrun.

    On timeout the awaitable is cancelled and :exc:`TurnBudgetExceeded` is
    raised, so the *turn* unwinds and completes instead of parking forever. That
    completion is the whole point: it is what lets every reminder and coalesced
    fire queued behind this turn proceed.

    Uses :func:`asyncio.timeout` rather than :func:`asyncio.wait_for` for one
    reason: ``cm.expired()``. Both raise :exc:`TimeoutError` on overrun, but so
    does an inner call whose OWN client timeout fired (httpx maps its read
    timeout onto it), and ``wait_for`` gives no way to tell the two apart. That
    ambiguity matters operationally — reporting an upstream's timeout as
    ``budget_exceeded`` sends the operator to the wrong knob and hides a real
    remote fault. ``expired()`` answers precisely which deadline fired; an inner
    timeout propagates untouched, as any other error would.
    """
    budget = turn_op_timeout_seconds() if timeout is None else timeout
    try:
        async with asyncio.timeout(budget) as cm:
            return await awaitable
    except TimeoutError as exc:
        if not cm.expired():
            raise            # somebody else's timeout — do not claim it
        logger.warning(
            "actor_turn.budget_exceeded op=%s actor_id=%s timeout=%.0fs — "
            "turn released so the queue behind it can drain",
            op, actor_id, budget,
        )
        raise TurnBudgetExceeded(op, actor_id, budget) from exc


async def bounded_turn_op_or(
    awaitable: Awaitable[Any],
    default: Any,
    *,
    op: str,
    actor_id: str,
    timeout: float | None = None,
) -> Any:
    """:func:`bounded_turn_op`, returning ``default`` instead of raising.

    For the ops whose natural failure mode is already "we didn't get it" — a
    deps lookup that returns ``None``, a best-effort provisioning result — so
    the timeout path rejoins an existing, already-tested branch rather than
    inventing a second one.
    """
    try:
        return await bounded_turn_op(
            awaitable, op=op, actor_id=actor_id, timeout=timeout
        )
    except TurnBudgetExceeded:
        return default


# ---------------------------------------------------------------------------
# Heal circuit breaker
# ---------------------------------------------------------------------------


class HealBreaker:
    """Per-actor consecutive-timeout tracking for the reconciler's heal.

    ``ENSURE_ACTIVE`` is re-emitted for every active analyst and source on every
    periodic resync, so an actor that is not answering gets re-poked forever. A
    deadline alone bounds each poke but not their sum: at 217 active actors, a
    fleet-wide wedge still burns ``217 x heal_timeout`` of queue time per cycle,
    every cycle. The breaker converts that into a skip.

    Safe to skip, precisely because the heal is idempotent and re-runs on the
    next resync — this trades a delayed re-assert (bounded by the cooloff) for a
    reconcile loop that keeps converging everything else. Not used for
    CREATE/RETIRE/TRANSITION: those are one-shot convergence steps where a skip
    would mean a descriptor never reaching its declared state.

    Not thread-safe and does not need to be — the reconcile loop is a single
    coroutine on one event loop, and in multi-node it is leader-gated to one
    replica.
    """

    def __init__(
        self,
        *,
        trips: int | None = None,
        cooloff_seconds: float | None = None,
        clock: Any = None,
    ) -> None:
        self._trips_override = trips
        self._cooloff_override = cooloff_seconds
        self._clock = clock or time.monotonic
        #: actor_id -> (consecutive_timeouts, monotonic_ts_of_last_timeout)
        self._state: dict[str, tuple[int, float]] = {}

    # -- config ------------------------------------------------------------
    @property
    def trips(self) -> int:
        return (
            self._trips_override
            if self._trips_override is not None
            else heal_breaker_trips()
        )

    @property
    def cooloff(self) -> float:
        return (
            self._cooloff_override
            if self._cooloff_override is not None
            else float(heal_breaker_cooloff_seconds())
        )

    # -- protocol ----------------------------------------------------------
    def should_skip(self, actor_id: str) -> bool:
        """True when this actor's heal is suppressed right now.

        Open + inside the cooloff → skip. Open + past the cooloff → allow ONE
        probe through (the counter is left alone, so a probe that times out
        again simply re-stamps and re-opens; a probe that succeeds clears it).
        """
        entry = self._state.get(actor_id)
        if entry is None:
            return False
        count, last = entry
        if count < self.trips:
            return False
        return (self._clock() - last) < self.cooloff

    def record_timeout(self, actor_id: str) -> int:
        """Count one heal timeout. Returns the new consecutive count."""
        count, _ = self._state.get(actor_id, (0, 0.0))
        count += 1
        self._evict_if_full(actor_id)
        self._state[actor_id] = (count, self._clock())
        return count

    def record_success(self, actor_id: str) -> None:
        """Clear this actor — the heal confirmed, so the streak is broken."""
        self._state.pop(actor_id, None)

    def forget(self, actor_id: str) -> None:
        """Drop tracking entirely (the actor was retired / superseded)."""
        self._state.pop(actor_id, None)

    def open_actors(self) -> list[str]:
        """Actor ids whose breaker is currently open — for diagnostics."""
        trips = self.trips
        return sorted(
            aid for aid, (count, _) in self._state.items() if count >= trips
        )

    # -- internals ---------------------------------------------------------
    def _evict_if_full(self, incoming: str) -> None:
        """Bound the map. Only failing actors are tracked and success clears
        them, so this should never fire in practice — it exists so that a
        pathological churn of actor ids cannot turn a leak-prevention mechanism
        into a leak. Evicts the least-recently-failed entry."""
        if incoming in self._state or len(self._state) < _MAX_TRACKED:
            return
        oldest = min(self._state.items(), key=lambda kv: kv[1][1])[0]
        self._state.pop(oldest, None)
        logger.warning(
            "actor_turn.heal_breaker.evicted actor_id=%s (tracking cap %d "
            "reached — this should not happen; investigate actor-id churn)",
            oldest, _MAX_TRACKED,
        )


__all__ = [
    "HEAL_BREAKER_COOLOFF_DEFAULT_S",
    "HEAL_BREAKER_COOLOFF_ENV",
    "HEAL_BREAKER_TRIPS_DEFAULT",
    "HEAL_BREAKER_TRIPS_ENV",
    "HEAL_TIMEOUT_DEFAULT_S",
    "HEAL_TIMEOUT_ENV",
    "TURN_OP_TIMEOUT_DEFAULT_S",
    "TURN_OP_TIMEOUT_ENV",
    "HealBreaker",
    "TurnBudgetExceeded",
    "bounded_turn_op",
    "bounded_turn_op_or",
    "heal_breaker_cooloff_seconds",
    "heal_breaker_trips",
    "heal_timeout_seconds",
    "turn_op_timeout_seconds",
]
