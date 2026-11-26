# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SW-8 — a blown heal deadline names what held the actor's turn.

The reconciler's ``ENSURE_ACTIVE`` heal pokes every active analyst and source
with ``activate`` once per resync under a 20 s deadline. It blows over a hundred times a day, and
the line said only that it blew. The witness now classifies every deadline:

  * ``queued_behind_turn`` — another call (a cadence run, a poll) holds the
    turn; ``held_by`` / ``held_for_s`` name it;
  * ``own_call_overran``   — the activate itself is executing; ``in_turn_op``
    names the bounded activation phase running (or ``None`` for an unbounded
    step, with ``last_turn_op`` saying which phase it was past);
  * ``not_delivered``      — no call for that actor is in this process.

Every case here goes through the REAL built ASGI stack with the witness
middleware mounted (the production binding) and the REAL
``execute_reconcile_action``; only daprd's turn queue is faked, by a proxy
whose ``activate`` waits for the held turn to end before entering the stack.
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from fastapi import FastAPI

from legba.runtime import actor_turn
from legba.runtime.actor_turn import HealBreaker, bounded_turn_op
from legba.runtime.actor_turn_witness import (
    HEAL_NOT_DELIVERED,
    HEAL_OWN_CALL_OVERRAN,
    HEAL_PHASES,
    HEAL_QUEUED_BEHIND_TURN,
    ActorTurnWitness,
    actor_turn_witness,
    heal_deadline_witness,
    method_from_path,
    mount_actor_turn_witness,
)
from legba.runtime.dapr_host import execute_reconcile_action
from legba.runtime.reconcile import ActionKind, ReconcileAction

ACTOR = "analyst::fact_contention_arbiter::f896902baa5bcf48"
PHASE_OP = "analyst.activate.resolve_deps"


@pytest.fixture(autouse=True)
def _clean_witness():
    """The witness is process-wide; leave it as we found it."""
    w = actor_turn_witness()
    saved = dict(w._state)
    w._state.clear()
    yield w
    w._state.clear()
    w._state.update(saved)


def _scope(path: str) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.1"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
    }


async def _call_stack(stack, path: str) -> list[dict]:
    messages: list[dict] = []

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        messages.append(message)

    await stack(_scope(path), _receive, _send)
    return messages


def _actor_host(*, run_release: asyncio.Event, activate_hangs: bool) -> FastAPI:
    """A host app with the dapr callback shapes and the witness mounted.

    ``remind/run_cadence`` holds its turn until ``run_release`` is set (a long
    cadence run). ``activate`` runs the real bounded deps phase — hung when
    ``activate_hangs``, instant otherwise.
    """
    app = FastAPI()

    @app.put("/actors/{actor_type}/{actor_id}/method/remind/{name}")
    async def _remind(actor_type: str, actor_id: str, name: str) -> dict:
        await run_release.wait()
        return {}

    @app.put("/actors/{actor_type}/{actor_id}/method/{method}")
    async def _method(actor_type: str, actor_id: str, method: str) -> dict:
        async def _deps():
            if activate_hangs:
                await asyncio.Event().wait()
            return object()

        await bounded_turn_op(_deps(), op=PHASE_OP, actor_id=actor_id, timeout=60)
        return {}

    mount_actor_turn_witness(app)
    return app


class _DaprQueuedProxy:
    """daprd's turn queue: ``activate`` enters the host only once the actor's
    held turn (the cadence run) has ended. The host serves it as its OWN
    request (a task), as over the wire: the caller giving up does not cancel
    the call daprd already delivered."""

    def __init__(self, stack, run_done: asyncio.Event | None) -> None:
        self._stack = stack
        self._run_done = run_done
        self.delivered: asyncio.Task | None = None

    async def activate(self) -> None:
        if self._run_done is not None:
            await self._run_done.wait()
        self.delivered = asyncio.create_task(
            _call_stack(self._stack, f"/actors/AnalystActor/{ACTOR}/method/activate"),
        )
        await asyncio.shield(self.delivered)


def _ensure_active() -> ReconcileAction:
    return ReconcileAction(
        kind=ActionKind.ENSURE_ACTIVE,
        actor_id=ACTOR,
        detail={
            "descriptor_id": "fact_contention_arbiter",
            "descriptor_kind": "analyst",
            "descriptor_version": "0" * 16,
        },
    )


def _fields(rec: logging.LogRecord) -> dict:
    """The witness fields as they ride on the record (structured extras)."""
    return {
        k: getattr(rec, k)
        for k in ("heal_phase", "held_by", "held_for_s", "in_turn_op", "in_turn_op_ms")
    }


def _deadline_records(caplog) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.getMessage().startswith("action_executor.deadline ")
    ]


# ---------------------------------------------------------------------------
# The witness's own vocabulary
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "label"),
    [
        (f"/actors/AnalystActor/{ACTOR}/method/activate", "activate"),
        (f"/actors/AnalystActor/{ACTOR}/method/run", "run"),
        (f"/actors/AnalystActor/{ACTOR}/method/remind/run_cadence", "remind/run_cadence"),
        (f"/actors/SourceActor/{ACTOR}/method/timer/t1", "timer/t1"),
        (f"/actors/AnalystActor/{ACTOR}", "deactivate"),
    ],
)
def test_method_label_for_every_callback_shape(path, label):
    assert method_from_path(path) == label


def test_unknown_actor_snapshot_is_absence_not_zero():
    snap = ActorTurnWitness().turn_snapshot(ACTOR)
    assert snap["in_flight"] == 0
    assert all(snap[k] is None for k in snap if k != "in_flight")
    assert heal_deadline_witness(ACTOR, "activate", witness=ActorTurnWitness())[
        "heal_phase"
    ] == HEAL_NOT_DELIVERED


def test_the_oldest_in_flight_call_is_the_holder():
    w = ActorTurnWitness()
    w.begin(ACTOR, "remind/run_cadence")
    w.begin(ACTOR, "activate")
    assert w.turn_snapshot(ACTOR)["held_by"] == "remind/run_cadence"
    assert heal_deadline_witness(ACTOR, "activate", witness=w)["heal_phase"] == (
        HEAL_QUEUED_BEHIND_TURN
    )
    w.end(ACTOR, "remind/run_cadence")
    assert w.turn_snapshot(ACTOR)["held_by"] == "activate"
    assert heal_deadline_witness(ACTOR, "activate", witness=w)["heal_phase"] == (
        HEAL_OWN_CALL_OVERRAN
    )
    w.end(ACTOR, "activate")
    assert w.turn_snapshot(ACTOR)["in_flight"] == 0


def test_a_caller_side_bounded_op_marks_no_phase(_clean_witness):
    """The reconciler's own ``reconcile.activate`` names the actor but runs in
    no turn — it must never be read as an activation phase."""
    w = _clean_witness
    w.begin(ACTOR, "remind/run_cadence")   # the actor IS busy, elsewhere

    async def _body():
        await bounded_turn_op(asyncio.sleep(0), op="reconcile.activate", actor_id=ACTOR)

    asyncio.run(_body())
    snap = w.turn_snapshot(ACTOR)
    assert snap["in_turn_op"] is None and snap["last_turn_op"] is None


async def test_an_in_turn_bounded_op_is_marked_through_the_real_stack():
    seen: list[dict] = []
    app = FastAPI()

    @app.put("/actors/{actor_type}/{actor_id}/method/{method}")
    async def _activate(actor_type: str, actor_id: str, method: str) -> dict:
        async def _deps():
            seen.append(actor_turn_witness().turn_snapshot(actor_id))
            return 1

        await bounded_turn_op(_deps(), op=PHASE_OP, actor_id=actor_id)
        seen.append(actor_turn_witness().turn_snapshot(actor_id))
        return {}

    mount_actor_turn_witness(app)
    await _call_stack(
        app.build_middleware_stack(), f"/actors/AnalystActor/{ACTOR}/method/activate",
    )
    during, after = seen
    assert during["held_by"] == "activate" and during["in_turn_op"] == PHASE_OP
    assert during["in_turn_op_ms"] is not None and during["in_turn_op_ms"] >= 0
    assert after["in_turn_op"] is None and after["last_turn_op"] == PHASE_OP
    assert actor_turn_witness().turn_snapshot(ACTOR)["in_flight"] == 0


# ---------------------------------------------------------------------------
# The deadline line, end to end through execute_reconcile_action
# ---------------------------------------------------------------------------


async def test_heal_queued_behind_a_cadence_run_says_so(monkeypatch, caplog):
    """The live shape (10-03 18:38:41Z): the arbiter's run_cadence fired at
    18:37:00 and held the turn to 18:39:01; the resync's activate waited 20 s
    behind it."""
    monkeypatch.setenv(actor_turn.HEAL_TIMEOUT_ENV, "1")
    release, run_done = asyncio.Event(), asyncio.Event()
    stack = _actor_host(run_release=release, activate_hangs=False).build_middleware_stack()

    async def _cadence_run():
        await _call_stack(stack, f"/actors/AnalystActor/{ACTOR}/method/remind/run_cadence")
        run_done.set()

    run = asyncio.create_task(_cadence_run())
    await asyncio.sleep(0.05)                    # the run holds the turn
    caplog.set_level(logging.WARNING, logger="legba.runtime.dapr_host")
    await execute_reconcile_action(
        _ensure_active(),
        proxy_for=lambda kind, aid: _DaprQueuedProxy(stack, run_done),
        breaker=HealBreaker(),
    )
    release.set()
    await run

    (rec,) = _deadline_records(caplog)
    fields = _fields(rec)
    assert fields["heal_phase"] == HEAL_QUEUED_BEHIND_TURN
    assert fields["held_by"] == "remind/run_cadence"
    assert fields["held_for_s"] >= 1.0
    assert fields["in_turn_op"] is None
    msg = rec.getMessage()
    assert msg.startswith(
        f"action_executor.deadline kind=ENSURE_ACTIVE method=activate actor_id={ACTOR} "
        "consecutive=1 "
    )
    assert "heal_phase=queued_behind_turn in_flight=1 held_by=remind/run_cadence" in msg


async def test_an_overrunning_activation_names_its_phase(monkeypatch, caplog):
    """The 08-01 class: the activate reached the actor and its deps phase hung."""
    monkeypatch.setenv(actor_turn.HEAL_TIMEOUT_ENV, "1")
    stack = _actor_host(
        run_release=asyncio.Event(), activate_hangs=True,
    ).build_middleware_stack()
    caplog.set_level(logging.WARNING, logger="legba.runtime.dapr_host")
    proxy = _DaprQueuedProxy(stack, None)
    await execute_reconcile_action(
        _ensure_active(), proxy_for=lambda kind, aid: proxy, breaker=HealBreaker(),
    )
    (rec,) = _deadline_records(caplog)
    fields = _fields(rec)
    assert fields["heal_phase"] == HEAL_OWN_CALL_OVERRAN
    assert fields["held_by"] == "activate"
    assert fields["in_turn_op"] == PHASE_OP
    assert fields["in_turn_op_ms"] >= 900
    assert f"in_turn_op={PHASE_OP}" in rec.getMessage()
    # The delivered call is still in the host; ending it releases the witness.
    proxy.delivered.cancel()
    with pytest.raises(asyncio.CancelledError):
        await proxy.delivered
    snap = actor_turn_witness().turn_snapshot(ACTOR)
    assert snap["in_flight"] == 0 and snap["last_turn_op"] == PHASE_OP


async def test_a_heal_that_never_reached_the_host_is_not_delivered(monkeypatch, caplog):
    monkeypatch.setenv(actor_turn.HEAL_TIMEOUT_ENV, "1")

    class _Undelivered:
        async def activate(self) -> None:
            await asyncio.Event().wait()

    caplog.set_level(logging.WARNING, logger="legba.runtime.dapr_host")
    await execute_reconcile_action(
        _ensure_active(), proxy_for=lambda kind, aid: _Undelivered(),
        breaker=HealBreaker(),
    )
    (rec,) = _deadline_records(caplog)
    assert rec.heal_phase == HEAL_NOT_DELIVERED
    assert rec.held_by is None
    assert "held_for_s=None" in rec.getMessage()     # absence, never 0
    # The production JSON formatter carries the fields as a structured object.
    import json

    from legba.runtime.logging_setup import JsonLogFormatter

    payload = json.loads(JsonLogFormatter().format(rec))
    assert payload["heal_phase"] == HEAL_NOT_DELIVERED
    assert payload["held_for_s"] is None
    assert set(HEAL_PHASES) == {
        HEAL_QUEUED_BEHIND_TURN, HEAL_OWN_CALL_OVERRAN, HEAL_NOT_DELIVERED,
    }
