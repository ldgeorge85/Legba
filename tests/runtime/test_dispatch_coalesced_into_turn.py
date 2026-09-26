# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A coalesced re-fire onto a busy actor is not a failed run.

THE DEFECT. Dapr actors are turn-based with reentrancy off, so a coalesced
re-fire dispatched onto a per-(analyst, target) worker that is already inside a
turn — its cadence fan-out run, a heal, an earlier fire — waits behind it. When
that wait outlasts the invoke line the client raises, and the trigger plane
logged ``trigger.run.failed`` at ERROR: an assertion that the analyst failed,
made by the one party that did not find out. The run's own ``analyst_traces``
row says ``success``. The live ledger recorded the class on 2026-09-24 (two
dispatch-side timeouts on ``narrative_coordination/country_watch_ir`` while the
runs themselves succeeded in 37-44 s) and it recurs at roughly six a day.

THE CONTRACT, which is what these tests pin — not a bigger budget (that was the
previous attempt; see ``test_actor_invoke_timeout``) and not the invoke line:

  * ``AnalystActor.run`` reports its OWN failures by returning an outcome, so an
    exception out of the invoke is always about the transport or the turn queue,
    never about the analyst;
  * the actor turn witness says whether that actor id was occupied across this
    fire — it is fed by the actor host's own callback path, so "a request for
    this actor id is in flight here" is "this actor's turn is occupied";
  * occupied → :class:`DispatchCoalesced` → ``trigger.coalesced_into_turn`` at
    INFO, ``status="coalesced"``;
  * idle → the exception stands → ``trigger.run.failed`` at ERROR, which now
    means work was actually lost.

The last bullet is the one that has to keep working, so it is tested twice: a
witness with no evidence must never soften a real failure.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import pytest

import legba.runtime.source_first_runtime as sfr
from legba.runtime.actor_turn_witness import (
    COVER_ENDED_AFTER_FIRE,
    COVER_IN_FLIGHT,
    ActorTurnWitness,
    ActorTurnWitnessMiddleware,
    actor_id_from_path,
    actor_turn_witness,
    mount_actor_turn_witness,
)
from legba.runtime.triggers.dispatch import (
    ActorTriggerRunner,
    DispatchCoalesced,
    TriggerFire,
)
from legba.runtime.triggers.policy import TriggerReason

FIRED_AT = datetime(2026, 9, 24, 23, 41, 58, tzinfo=timezone.utc)
WORKER_ID = "analyst::narrative_coordination::country_watch_ir"


def _fire(analyst: str = "narrative_coordination", target: str = "country_watch_ir"):
    return TriggerFire(
        analyst_id=analyst,
        target_id=target,
        tenant="shared",
        reason=TriggerReason.ACCUMULATION,
        pending_count=4,
        severity_wake=False,
        fired_at=FIRED_AT,
    )


@pytest.fixture(autouse=True)
def _isolate_live_set():
    """Snapshot/restore the module-global dispatch live-set around each test."""
    saved = dict(sfr._ANALYST_ACTOR_IDS)
    sfr._ANALYST_ACTOR_IDS.clear()
    yield
    sfr._ANALYST_ACTOR_IDS.clear()
    sfr._ANALYST_ACTOR_IDS.update(saved)


@pytest.fixture(autouse=True)
def _clean_witness():
    """The witness is process-wide; leave it as we found it."""
    w = actor_turn_witness()
    saved = dict(w._state)
    w._state.clear()
    yield w
    w._state.clear()
    w._state.update(saved)


# ---------------------------------------------------------------------------
# actor_id_from_path — every shape daprd calls the host back on is a turn
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/actors/AnalystActor/analyst%3A%3Aescalation%3A%3Acountry_g20_id/method/run",
        "/actors/AnalystActor/analyst::escalation::country_g20_id/method/run",
        "/actors/AnalystActor/analyst::escalation::country_g20_id/method/remind/run_cadence",
        "/actors/AnalystActor/analyst::escalation::country_g20_id/method/timer/t1",
        "/actors/AnalystActor/analyst::escalation::country_g20_id",
    ],
)
def test_actor_id_recovered_from_every_callback_shape(path):
    assert actor_id_from_path(path) == "analyst::escalation::country_g20_id"


@pytest.mark.parametrize("path", ["/healthz", "/", "/a2a/skills/x", "/actors", ""])
def test_non_actor_paths_are_not_turns(path):
    assert actor_id_from_path(path) is None


# ---------------------------------------------------------------------------
# ActorTurnWitness — what it will and will not claim
# ---------------------------------------------------------------------------


def test_unknown_actor_is_never_covered():
    assert ActorTurnWitness().covers(WORKER_ID, since=FIRED_AT) is None


def test_in_flight_turn_covers_the_fire():
    w = ActorTurnWitness()
    w.begin(WORKER_ID)
    assert w.covers(WORKER_ID, since=FIRED_AT) == COVER_IN_FLIGHT
    assert w.in_flight(WORKER_ID) == 1


def test_turn_that_ended_after_the_fire_covers_it():
    w = ActorTurnWitness()
    w.begin(WORKER_ID)
    w.end(WORKER_ID)
    # The fire is long in the past; this turn ended just now, i.e. after it.
    assert w.covers(WORKER_ID, since=FIRED_AT) == COVER_ENDED_AFTER_FIRE


def test_turn_that_ended_before_the_fire_does_not_cover_it():
    """The honesty guard: a turn finished before the fire cannot have absorbed it."""
    w = ActorTurnWitness()
    w.begin(WORKER_ID)
    w.end(WORKER_ID)
    later = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
    assert w.covers(WORKER_ID, since=later) is None


def test_queued_turns_count_down_one_at_a_time():
    w = ActorTurnWitness()
    w.begin(WORKER_ID)
    w.begin(WORKER_ID)
    w.end(WORKER_ID)
    assert w.in_flight(WORKER_ID) == 1
    assert w.covers(WORKER_ID, since=FIRED_AT) == COVER_IN_FLIGHT
    w.end(WORKER_ID)
    assert w.in_flight(WORKER_ID) == 0


def test_end_without_begin_cannot_fabricate_coverage():
    w = ActorTurnWitness()
    w.end(WORKER_ID)
    assert w.covers(WORKER_ID, since=FIRED_AT) is None
    assert w.in_flight(WORKER_ID) == 0


def test_finished_turns_are_pruned_so_the_witness_cannot_leak():
    w = ActorTurnWitness(retention_seconds=0.0)
    for i in range(5):
        w.begin(f"analyst::a::t{i}")
        w.end(f"analyst::a::t{i}")
    # A new id triggers the prune; the finished, out-of-retention ones go.
    w.begin("analyst::a::live")
    assert w.tracked() == 1
    assert w.in_flight("analyst::a::live") == 1


# ---------------------------------------------------------------------------
# The middleware — the real ASGI binding path
# ---------------------------------------------------------------------------


async def test_middleware_marks_the_turn_for_the_duration_of_the_call():
    w = ActorTurnWitness()
    observed: list[int] = []

    async def _app(scope, receive, send):
        observed.append(w.in_flight(WORKER_ID))

    mw = ActorTurnWitnessMiddleware(_app, witness=w)
    scope = {"type": "http", "path": f"/actors/AnalystActor/{WORKER_ID}/method/run"}
    await mw(scope, None, None)

    assert observed == [1]           # occupied while the turn ran
    assert w.in_flight(WORKER_ID) == 0
    assert w.covers(WORKER_ID, since=FIRED_AT) == COVER_ENDED_AFTER_FIRE


async def test_middleware_releases_the_turn_when_the_call_raises():
    """A dropped/failed turn must still end, or the witness would excuse everything."""
    w = ActorTurnWitness()

    async def _app(scope, receive, send):
        raise RuntimeError("connection reset by peer")

    mw = ActorTurnWitnessMiddleware(_app, witness=w)
    scope = {"type": "http", "path": f"/actors/AnalystActor/{WORKER_ID}/method/run"}
    with pytest.raises(RuntimeError):
        await mw(scope, None, None)
    assert w.in_flight(WORKER_ID) == 0


async def test_middleware_passes_non_actor_traffic_through_unwitnessed():
    w = ActorTurnWitness()
    calls: list[str] = []

    async def _app(scope, receive, send):
        calls.append(scope["path"])

    mw = ActorTurnWitnessMiddleware(_app, witness=w)
    await mw({"type": "http", "path": "/healthz"}, None, None)
    # A non-http scope (lifespan, websocket) is not a turn even on an actor path.
    await mw({"type": "lifespan", "path": "/actors/x/y/method/run"}, None, None)
    assert calls == ["/healthz", "/actors/x/y/method/run"]
    assert w.tracked() == 0


def test_mount_attaches_the_middleware_to_a_real_app():
    from fastapi import FastAPI

    app = FastAPI()
    mount_actor_turn_witness(app)
    assert ActorTurnWitnessMiddleware in [m.cls for m in app.user_middleware]


async def test_mounted_app_witnesses_a_real_actor_request():
    """Through the built ASGI stack, not the middleware class in isolation."""
    from fastapi import FastAPI

    app = FastAPI()
    seen: list[int] = []

    @app.put("/actors/{actor_type}/{actor_id}/method/{method}")
    async def _run(actor_type: str, actor_id: str, method: str) -> dict:
        seen.append(actor_turn_witness().in_flight(WORKER_ID))
        return {"outcome": "ok"}

    mount_actor_turn_witness(app)

    stack = app.build_middleware_stack()
    messages: list[dict] = []

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        messages.append(message)

    await stack(
        {
            "type": "http",
            "app": app,
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "method": "PUT",
            "scheme": "http",
            "path": f"/actors/AnalystActor/{WORKER_ID}/method/run",
            "raw_path": f"/actors/AnalystActor/{WORKER_ID}/method/run".encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        },
        _receive,
        _send,
    )

    assert [m["status"] for m in messages if m["type"] == "http.response.start"] == [200]
    assert seen == [1]                                   # occupied inside the turn
    assert actor_turn_witness().in_flight(WORKER_ID) == 0  # released after it


# ---------------------------------------------------------------------------
# build_trigger_work — the real dispatch seam
# ---------------------------------------------------------------------------


def _patch_proxy(monkeypatch, run):
    """Swap ActorProxy.create for a proxy whose ``run`` is ``run``."""
    import dapr.actor as da

    class _FakeProxy:
        async def run(self, payload):
            return await run(payload)

    monkeypatch.setattr(
        da.ActorProxy, "create", staticmethod(lambda *a, **k: _FakeProxy())
    )


async def test_invoke_failure_on_a_busy_actor_raises_dispatch_coalesced(monkeypatch):
    sfr.remember_analyst_actor_id(
        "narrative_coordination", "analyst::narrative_coordination::abc"
    )

    async def _blows_up(payload):
        raise TimeoutError("invoke line expired waiting for the turn")

    _patch_proxy(monkeypatch, _blows_up)
    # The turn the re-fire queued behind is still running.
    actor_turn_witness().begin(WORKER_ID)

    work = sfr.build_trigger_work(None)
    with pytest.raises(DispatchCoalesced) as caught:
        await work(_fire())

    absorbed = caught.value
    assert absorbed.witness == COVER_IN_FLIGHT
    assert absorbed.analyst_id == "narrative_coordination"
    assert absorbed.target_id == "country_watch_ir"
    assert "TimeoutError" in absorbed.transport
    # The transport error is kept as the cause — a coalesce that is really a
    # sick sidecar stays diagnosable.
    assert isinstance(absorbed.__cause__, TimeoutError)


async def test_invoke_failure_after_the_turn_finished_is_also_coalesced(monkeypatch):
    sfr.remember_analyst_actor_id(
        "narrative_coordination", "analyst::narrative_coordination::abc"
    )

    async def _blows_up(payload):
        raise RuntimeError("ERR_ACTOR_INVOKE_METHOD")

    _patch_proxy(monkeypatch, _blows_up)
    w = actor_turn_witness()
    w.begin(WORKER_ID)
    w.end(WORKER_ID)          # the turn completed — after this fire was raised

    work = sfr.build_trigger_work(None)
    with pytest.raises(DispatchCoalesced) as caught:
        await work(_fire())
    assert caught.value.witness == COVER_ENDED_AFTER_FIRE


async def test_invoke_failure_on_an_idle_actor_propagates_unchanged(monkeypatch):
    """No witness evidence ⇒ no excuse. This is the regression that matters."""
    sfr.remember_analyst_actor_id(
        "narrative_coordination", "analyst::narrative_coordination::abc"
    )

    boom = RuntimeError("connection reset by peer")

    async def _blows_up(payload):
        raise boom

    _patch_proxy(monkeypatch, _blows_up)

    work = sfr.build_trigger_work(None)
    with pytest.raises(RuntimeError) as caught:
        await work(_fire())
    assert caught.value is boom


async def test_a_completed_invoke_is_untouched_by_the_witness(monkeypatch):
    sfr.remember_analyst_actor_id(
        "narrative_coordination", "analyst::narrative_coordination::abc"
    )

    async def _ok(payload):
        return {"outcome": "success"}

    _patch_proxy(monkeypatch, _ok)
    actor_turn_witness().begin(WORKER_ID)   # busy, but the invoke completed

    work = sfr.build_trigger_work(None)
    out = await work(_fire())
    assert out == {
        "actor_run": {"outcome": "success"},
        "target_id": "country_watch_ir",
    }


async def test_cancellation_is_never_reinterpreted_as_a_coalesce(monkeypatch):
    """Engine teardown must keep propagating as cancellation."""
    import asyncio

    sfr.remember_analyst_actor_id(
        "narrative_coordination", "analyst::narrative_coordination::abc"
    )

    async def _cancelled(payload):
        raise asyncio.CancelledError()

    _patch_proxy(monkeypatch, _cancelled)
    actor_turn_witness().begin(WORKER_ID)

    work = sfr.build_trigger_work(None)
    with pytest.raises(asyncio.CancelledError):
        await work(_fire())


# ---------------------------------------------------------------------------
# ActorTriggerRunner — the log vocabulary
# ---------------------------------------------------------------------------


async def test_coalesced_fire_logs_info_not_run_failed(caplog):
    async def _work(fire):
        raise DispatchCoalesced(
            analyst_id=fire.analyst_id,
            target_id=fire.target_id,
            witness=COVER_IN_FLIGHT,
            transport="TimeoutError: invoke line expired",
        )

    runner = ActorTriggerRunner(_work)
    with caplog.at_level(logging.INFO, logger="legba.runtime.triggers.dispatch"):
        res = await runner.run(_fire())

    assert res.status == "coalesced"
    assert res.error is None
    assert res.detail == {"witness": COVER_IN_FLIGHT}
    assert res.pending_count == 4
    assert runner.coalesced == 1

    text = caplog.text
    assert "trigger.coalesced_into_turn" in text
    assert "witness=in_flight" in text
    assert "trigger.run.failed" not in text
    assert [r.levelno for r in caplog.records] == [logging.INFO]


async def test_a_real_dispatch_failure_still_logs_run_failed(caplog):
    async def _work(fire):
        raise RuntimeError("connection reset by peer")

    runner = ActorTriggerRunner(_work)
    with caplog.at_level(logging.INFO, logger="legba.runtime.triggers.dispatch"):
        res = await runner.run(_fire())

    assert res.status == "failed"
    assert "connection reset by peer" in (res.error or "")
    assert runner.coalesced == 0
    assert "trigger.run.failed" in caplog.text
    assert "trigger.coalesced_into_turn" not in caplog.text
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_dispatch_and_runner_compose_end_to_end(monkeypatch, caplog):
    """The whole seam: a busy worker + a failed invoke ⇒ one INFO, no ERROR."""
    sfr.remember_analyst_actor_id(
        "narrative_coordination", "analyst::narrative_coordination::abc"
    )

    async def _blows_up(payload):
        raise TimeoutError("invoke line expired waiting for the turn")

    _patch_proxy(monkeypatch, _blows_up)
    actor_turn_witness().begin(WORKER_ID)

    runner = ActorTriggerRunner(sfr.build_trigger_work(None))
    with caplog.at_level(logging.INFO, logger="legba.runtime.triggers.dispatch"):
        res = await runner.run(_fire())

    assert res.status == "coalesced"
    assert "trigger.coalesced_into_turn" in caplog.text
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
