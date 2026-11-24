# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane N-U — idle deactivation of an actor this process never activated.

Live 11-13: the daprd sidecar outlives each runtime roll, so ~an hour after
the 13:21 roll it sent ``DELETE /actors/AnalystActor/<id>`` for 44 actors the
PRE-roll process had activated; the dapr SDK raised ``ValueError('<id> is not
activated')`` and ``dapr.ext.fastapi`` answered a bare 500 (sidecar: "Failed
to halt actor ... is not activated"; 4,859 of them in the collected sidecar log
since 08-01). Reproduced here on the SDK's own route; the mounted route answers
200 for that case, still deactivates a live actor, and logs a real failure.
"""
from __future__ import annotations

import logging

import httpx
import pytest
from dapr.actor import Actor, ActorInterface, actormethod
from dapr.actor.runtime.runtime import ActorRuntime
from dapr.ext.fastapi import DaprActor
from fastapi import FastAPI

from legba.runtime.actor_deactivation import mount_idempotent_deactivation

DEACTIVATED: list[str] = []


class _PingInterface(ActorInterface):
    @actormethod(name="Ping")
    async def ping(self) -> str: ...


class NUPingActor(Actor, _PingInterface):
    async def ping(self) -> str:
        return "pong"

    async def _on_deactivate(self) -> None:
        DEACTIVATED.append(self.id.id)


class NUBrokenActor(Actor, _PingInterface):
    async def ping(self) -> str:
        return "pong"

    async def _on_deactivate(self) -> None:
        raise RuntimeError("flush failed")


def _app(*, fixed: bool) -> FastAPI:
    app = FastAPI()
    if fixed:
        mount_idempotent_deactivation(app)
    DaprActor(app)
    return app


@pytest.fixture(autouse=True)
async def _registered():
    await ActorRuntime.register_actor(NUPingActor)
    await ActorRuntime.register_actor(NUBrokenActor)
    DEACTIVATED.clear()
    yield


async def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_the_sdk_route_answers_500_for_a_never_activated_actor():
    async with await _client(_app(fixed=False)) as c:
        r = await c.delete("/actors/NUPingActor/analyst%3A%3Adisruption_status%3A%3Ax")
    assert r.status_code == 500 and "is not activated" in r.json()["message"]


async def test_a_never_activated_actor_deactivates_as_a_200_no_op(caplog):
    caplog.set_level(logging.INFO, logger="legba.runtime.actor_deactivation")
    async with await _client(_app(fixed=True)) as c:
        r = await c.delete("/actors/NUPingActor/analyst%3A%3Adisruption_status%3A%3Ay")
    assert r.status_code == 200 and "not active" in r.json()["message"]
    assert any("deactivate_not_active" in m for m in caplog.messages)
    assert DEACTIVATED == []


async def test_an_activated_actor_still_deactivates_once():
    async with await _client(_app(fixed=True)) as c:
        r = await c.put("/actors/NUPingActor/live-1/method/Ping")
        assert r.status_code == 200
        r = await c.delete("/actors/NUPingActor/live-1")
        assert r.status_code == 200 and r.json()["message"].startswith("deactivated actor")
        assert DEACTIVATED == ["live-1"]
        # a second DELETE (daprd retry) is the not-active no-op
        r = await c.delete("/actors/NUPingActor/live-1")
        assert r.status_code == 200 and DEACTIVATED == ["live-1"]


async def test_a_real_deactivation_failure_is_logged_with_its_traceback(caplog):
    caplog.set_level(logging.INFO, logger="legba.runtime.actor_deactivation")
    async with await _client(_app(fixed=True)) as c:
        await c.put("/actors/NUBrokenActor/b-1/method/Ping")
        r = await c.delete("/actors/NUBrokenActor/b-1")
    assert r.status_code == 500 and "flush failed" in r.json()["message"]
    rec = [x for x in caplog.records if "deactivate_failed" in x.getMessage()]
    assert rec and rec[0].exc_info is not None


async def test_an_unknown_actor_type_is_still_a_500():
    async with await _client(_app(fixed=True)) as c:
        r = await c.delete("/actors/NoSuchActorType/x")
    assert r.status_code == 500


def test_the_host_app_matches_the_idempotent_route_first():
    from legba.runtime import dapr_host
    from legba.runtime.actor_deactivation import deactivate_actor

    app = dapr_host.build_dapr_host_app()
    deletes = [r for r in app.router.routes
               if getattr(r, "path", "") == "/actors/{actor_type_name}/{actor_id}"
               and "DELETE" in getattr(r, "methods", ())]
    # Ours is the first DELETE route on that path (the SDK's, where it is
    # mounted as a plain route, sits behind it).
    assert deletes and deletes[0].endpoint is deactivate_actor
    # the production app (main) mounts it the same way, before DaprActor(app)
    src = open(dapr_host.__file__, encoding="utf-8").read()
    assert src.count("mount_idempotent_deactivation(app)") == 2
    for chunk in src.split("mount_idempotent_deactivation(app)")[1:]:
        nxt = [ln.strip() for ln in chunk.splitlines()[1:] if ln.strip()
               and not ln.strip().startswith("#")]
        # roll 86: the runtime's /healthz may sit between them — it too must
        # precede DaprActor (whose own /healthz would answer first)
        lead = [ln for ln in nxt if not ln.startswith("mount_runtime_healthz(")]
        assert lead[0] == "DaprActor(app)"
        assert nxt.index(lead[0]) <= 1
