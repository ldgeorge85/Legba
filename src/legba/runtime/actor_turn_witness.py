# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who is holding an actor turn — the witness the trigger plane asks.

WHY THIS EXISTS. A coalesced re-fire is dispatched by awaiting an ActorProxy
``run`` on the per-(analyst, target) worker actor. Dapr actors are turn-based
with reentrancy off, so when that actor is already inside a turn — its cadence
fan-out run, a heal, an earlier fire — our invoke waits behind it. If the wait
plus the run exceeds the invoke line the client raises, and
:class:`~.triggers.dispatch.ActorTriggerRunner` logged ``trigger.run.failed``:
an ERROR asserting the analyst failed, when in fact the dispatcher simply never
learned how the run ended. The run's own trace row says ``success``.

That is a dishonest log line, and its cost is not cosmetic — an ERROR that is
routinely wrong is an ERROR nobody reads, which is how a real dispatch loss
hides. The fix is not a bigger budget (that was tried; see
``source_first_runtime.actor_invoke_timeout_seconds``) but a contract that can
tell the two apart:

  * the target actor was occupied across our fire → the fire was **coalesced
    into the running turn**; log it at INFO and count it as such;
  * the target actor was idle → nothing ran, nothing will; ``run.failed``
    stands, and now means what it says.

WHAT THE WITNESS KNOWS. Every actor method call daprd makes — a reminder, a
fan-out ``run``, our own invoke — arrives at this process as an HTTP request to
``/actors/<Type>/<actor id>/…``. Dapr serialises those per actor id, so "a
request for this actor id is in flight here" *is* "this actor's turn is
occupied". :class:`ActorTurnWitnessMiddleware` records the boundary; the
dispatcher reads it. No new state store, no extra round trip, nothing on the
hot path but two dict writes.

WHAT IT DELIBERATELY DOES NOT KNOW. It is process-local, and it is a witness
rather than a ledger: it cannot see a turn taken by another replica, and an
unmounted middleware (a test host, a future split of the trigger plane off the
actor host) leaves it empty. Both degrade the same safe way — no evidence means
no coverage claim, so the caller falls back to the pre-existing
``run.failed``. It never invents coverage it did not observe.

Times are UTC epoch seconds, because the thing a caller compares against is a
fire's ``fired_at`` datetime and not a monotonic reading. A backwards clock step
can therefore only *lose* a coverage claim, never manufacture one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import unquote

logger = logging.getLogger(__name__)


#: Hard cap on tracked actor ids, so a witness can never become the leak it
#: exists to prevent. Mirrors ``actor_turn.HealBreaker``'s cap deliberately:
#: same plane, same reasoning, same order of magnitude of actors.
_MAX_TRACKED = 5_000

#: How long a COMPLETED turn stays visible to :meth:`ActorTurnWitness.covers`.
#: Only has to outlive the window between a fire and the invoke line giving up
#: on it (180 s by default), with room for a slow reconciliation; past that the
#: entry is dead weight and is pruned.
COMPLETED_TURN_RETENTION_S = 900.0


def _now_epoch() -> float:
    return datetime.now(tz=timezone.utc).timestamp()


class _Entry:
    """Per-actor turn state. Mutable and tiny — one of these per live actor."""

    __slots__ = ("in_flight", "started_at", "ended_at")

    def __init__(self) -> None:
        self.in_flight = 0
        self.started_at: float | None = None
        self.ended_at: float | None = None


#: The coverage verdicts :meth:`ActorTurnWitness.covers` can return. Strings
#: rather than an enum because their only consumer is a log field and a test
#: assertion, and a closed vocabulary of two reads better in a log line than
#: ``TurnCoverage.IN_FLIGHT``.
COVER_IN_FLIGHT = "in_flight"
COVER_ENDED_AFTER_FIRE = "ended_after_fire"


class ActorTurnWitness:
    """Records which actor ids are inside a turn in THIS process.

    Not thread-safe and does not need to be: the actor host is one event loop,
    and ``begin``/``end`` are straight-line dict updates with no await between
    them, so no other coroutine can interleave inside one.
    """

    def __init__(self, *, retention_seconds: float | None = None) -> None:
        self._retention = (
            COMPLETED_TURN_RETENTION_S
            if retention_seconds is None
            else float(retention_seconds)
        )
        self._state: dict[str, _Entry] = {}

    # -- recording ---------------------------------------------------------

    def begin(self, actor_id: str) -> None:
        """An actor method call for ``actor_id`` entered this process."""
        entry = self._state.get(actor_id)
        if entry is None:
            self._prune()
            entry = self._state.setdefault(actor_id, _Entry())
        entry.in_flight += 1
        entry.started_at = _now_epoch()

    def end(self, actor_id: str) -> None:
        """That call left. Counts down, so nested/queued calls stay honest."""
        entry = self._state.get(actor_id)
        if entry is None:
            return
        if entry.in_flight > 0:
            entry.in_flight -= 1
        entry.ended_at = _now_epoch()

    # -- reading -----------------------------------------------------------

    def covers(self, actor_id: str, *, since: datetime) -> str | None:
        """Was ``actor_id``'s turn occupied at or after ``since``?

        Returns :data:`COVER_IN_FLIGHT` when a call is in this process right
        now, :data:`COVER_ENDED_AFTER_FIRE` when the last one finished at or
        after ``since``, and ``None`` when there is no evidence either way.

        ``since`` is the fire's own ``fired_at``: a turn that ended BEFORE the
        fire was raised cannot have absorbed it, and saying otherwise would
        hand back the dishonest log line in a nicer costume.
        """
        entry = self._state.get(actor_id)
        if entry is None:
            return None
        if entry.in_flight > 0:
            return COVER_IN_FLIGHT
        if entry.ended_at is not None and entry.ended_at >= since.timestamp():
            return COVER_ENDED_AFTER_FIRE
        return None

    def in_flight(self, actor_id: str) -> int:
        """How many actor calls for this id are in this process (diagnostics)."""
        entry = self._state.get(actor_id)
        return entry.in_flight if entry is not None else 0

    def tracked(self) -> int:
        """How many actor ids the witness is holding (diagnostics/tests)."""
        return len(self._state)

    # -- internals ---------------------------------------------------------

    def _prune(self) -> None:
        """Drop finished, stale entries; evict the oldest if still at the cap.

        Called only when a NEW actor id appears, so steady state pays nothing.
        """
        now = _now_epoch()
        cutoff = now - self._retention
        stale = [
            aid
            for aid, e in self._state.items()
            if e.in_flight == 0 and (e.ended_at is None or e.ended_at < cutoff)
        ]
        for aid in stale:
            self._state.pop(aid, None)
        if len(self._state) < _MAX_TRACKED:
            return
        oldest = min(
            self._state.items(), key=lambda kv: kv[1].ended_at or kv[1].started_at or 0.0
        )[0]
        self._state.pop(oldest, None)
        logger.warning(
            "actor_turn_witness.evicted actor_id=%s (tracking cap %d reached — "
            "investigate actor-id churn)",
            oldest, _MAX_TRACKED,
        )


#: The process-wide witness. One per actor host; the middleware writes it and
#: the trigger dispatcher reads it.
_WITNESS = ActorTurnWitness()


def actor_turn_witness() -> ActorTurnWitness:
    """The process-wide :class:`ActorTurnWitness`."""
    return _WITNESS


def actor_id_from_path(path: str) -> str | None:
    """The actor id in a dapr actor-callback path, or ``None`` if it is not one.

    Dapr calls this app back at ``/actors/<Type>/<actor id>/method/<name>`` (and
    the ``method/remind/<name>`` / ``method/timer/<name>`` variants, plus a bare
    ``DELETE /actors/<Type>/<actor id>`` on deactivation). Every one of those is
    a turn, so the shape we key on is the first four segments and nothing
    deeper. The id is percent-encoded in the URL because our ids carry ``::``.
    """
    parts = path.split("/")
    # ["", "actors", "<Type>", "<id>", ...]
    if len(parts) < 4 or parts[1] != "actors":
        return None
    actor_id = unquote(parts[3])
    return actor_id or None


class ActorTurnWitnessMiddleware:
    """Pure-ASGI middleware stamping actor turns onto the witness.

    Pure ASGI rather than Starlette's ``BaseHTTPMiddleware`` on purpose: this
    sits in front of every actor invoke on the hot path, and
    ``BaseHTTPMiddleware`` wraps each request in an extra task plus a stream
    pair. This one adds two dict writes and a ``try/finally``.

    The ``finally`` is the load-bearing part — a turn that raised, was
    cancelled, or had its connection dropped mid-flight must still count as
    ended, or the witness would report an actor as permanently occupied and
    start excusing real failures.
    """

    def __init__(self, app: Any, witness: ActorTurnWitness | None = None) -> None:
        self._app = app
        self._witness = witness

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        actor_id = actor_id_from_path(scope.get("path") or "")
        if actor_id is None:
            await self._app(scope, receive, send)
            return
        witness = self._witness if self._witness is not None else actor_turn_witness()
        witness.begin(actor_id)
        try:
            await self._app(scope, receive, send)
        finally:
            witness.end(actor_id)


def mount_actor_turn_witness(app: Any) -> None:
    """Attach the witness middleware to a dapr-host app.

    Call once, immediately after ``DaprActor(app)`` and before startup —
    Starlette builds its middleware stack when the app starts.
    """
    app.add_middleware(ActorTurnWitnessMiddleware)


__all__ = [
    "COMPLETED_TURN_RETENTION_S",
    "COVER_ENDED_AFTER_FIRE",
    "COVER_IN_FLIGHT",
    "ActorTurnWitness",
    "ActorTurnWitnessMiddleware",
    "actor_id_from_path",
    "actor_turn_witness",
    "mount_actor_turn_witness",
]
