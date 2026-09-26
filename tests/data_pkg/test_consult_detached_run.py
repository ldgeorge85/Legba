# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The detached consult run — 202, the terminal frame, and the partial (D-7).

What broke on 2026-09-16
========================

``POST /api/v1/consult`` held the Dapr invoke open for the whole ReAct loop
under a 300s client timeout. A real consult ran past it; ``httpx`` closed the
connection; the handler raised 504. For a ``mode=chat`` run the registry is the
ONLY writer — the actor's chat branch returns its payload in the envelope and
writes no ``analyst_outputs`` row — so the answer had existed in exactly one
place, a response nobody was left to receive.

What these tests pin
====================

* **202 does not mean "nothing happened".** The run keeps going, the answer is
  persisted, and the terminal frame carries it to whoever is still listening.
* **The fast path is unchanged.** A run that finishes inside the sync window
  returns 200 with the same body it always did — no client has to change to
  keep working, which is what makes this deployable without a lockstep UI.
* **A failed run persists a PARTIAL, never nothing.** The operator contract is
  that the panel never goes blank: the turn says what landed, which round it
  died on, and why.
* **The answer survives cancellation.** ``_deliver`` runs under
  ``asyncio.shield``, so a cancel between the answer and the write cannot skip
  it — which is the exact gap the incident fell through.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import legba.data.registry.consult_api as consult_api
from legba.data.registry.api import RegistryAPIDeps
from legba.data.registry.consult_api import build_consult_router
from legba.data.registry.consult_runs import (
    ConsultRun,
    ConsultRunError,
    ConsultRunManager,
    publish_terminal_frame,
)

API_TOKEN_ENV = "LEGBA_REGISTRY_API_TOKEN"
TEST_TOKEN = "fake-detached-run-token"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Pg:
    """Records the audit writes ``consult_persistence`` issues."""

    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    def roles(self) -> list[str]:
        return [t["role"] for t in self.turns]

    def turn(self, role: str) -> dict[str, Any] | None:
        for t in self.turns:
            if t["role"] == role:
                return t
        return None

    def acquire(self) -> Any:
        pg = self

        class _Txn:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        class _Conn:
            def transaction(self) -> Any:
                return _Txn()

            async def fetchrow(self, sql: str, *args: Any) -> Any:
                if "INSERT INTO consult_sessions" in sql:
                    return {"id": "session-1"}
                if "INSERT INTO consult_turns" in sql:
                    pg.turns.append(
                        {
                            "session_id": args[0],
                            "role": args[1],
                            "content": args[2],
                            "steps": args[3],
                        }
                    )
                    return {"id": f"turn-{len(pg.turns)}"}
                raise AssertionError(f"unexpected SQL: {sql[:80]!r}")

            async def execute(self, sql: str, *args: Any) -> str:
                return "UPDATE 1"

        class _Ctx:
            async def __aenter__(self) -> Any:
                return _Conn()

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


class _DescRow:
    version = "v" + "b" * 16


class _Registry:
    def __init__(self, pg: Any) -> None:
        self.pg = pg

    async def get(self, descriptor_id: str, *, family: Any, version: Any = None) -> Any:
        return _DescRow()


class _Nc:
    """Core-NATS stand-in: records publishes, replays them to subscribers."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self._subs: dict[str, list[Any]] = {}

    async def publish(self, subject: str, payload: bytes) -> None:
        frame = json.loads(payload)
        self.published.append((subject, frame))
        for cb in self._subs.get(subject, []):
            await cb(type("_Msg", (), {"data": payload})())

    async def subscribe(self, subject: str, cb: Any = None) -> Any:
        self._subs.setdefault(subject, []).append(cb)
        nc = self

        class _Sub:
            async def unsubscribe(self) -> None:
                nc._subs.get(subject, []).remove(cb)

        return _Sub()

    def frames(self, kind: str) -> list[dict[str, Any]]:
        return [f for _s, f in self.published if f.get("type") == kind]


class _Nats:
    def __init__(self) -> None:
        self.nc = _Nc()


CHAT_ENVELOPE: dict[str, Any] = {
    "outcome": "success",
    "mode": "chat",
    "derived_from": [],
    "consult_response": {
        "answer": "the detached answer",
        "uncertainty": 0.3,
        "unanswered_aspects": [],
        "cited_substrate_refs": [],
        "data": {"steps": [{"phase": "act"}], "tool_calls": []},
    },
}


def _install_dapr(
    monkeypatch: pytest.MonkeyPatch,
    *,
    envelope: dict[str, Any] = CHAT_ENVELOPE,
    delay: float = 0.0,
    status_code: int = 200,
) -> None:
    class _Resp:
        def __init__(self) -> None:
            self.status_code = status_code

        def json(self) -> dict[str, Any]:
            return envelope

        @property
        def text(self) -> str:
            return json.dumps(envelope)

    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def put(self, url: str, json: Any = None, headers: Any = None) -> _Resp:
            if delay:
                await asyncio.sleep(delay)
            return _Resp()

    monkeypatch.setattr(consult_api.httpx, "AsyncClient", _Client)


def _app(pg: _Pg, nats: Any) -> FastAPI:
    app = FastAPI()
    deps = RegistryAPIDeps(
        descriptor_registry=_Registry(pg),  # type: ignore[arg-type]
        stack_registry=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        dlq=None,  # type: ignore[arg-type]
        audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=None,  # type: ignore[arg-type]
        nats_store=nats,
    )
    app.include_router(build_consult_router(deps), prefix="/api/v1")
    return app


def _post(client: TestClient, **body: Any) -> Any:
    payload = {"question": "what is happening?", "mode": "chat", "messages": []}
    payload.update(body)
    return client.post(
        "/api/v1/consult",
        json=payload,
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )


def _wait(predicate: Any, *, timeout: float = 5.0, what: str = "condition") -> None:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


# ---------------------------------------------------------------------------
# The two response paths
# ---------------------------------------------------------------------------


def test_a_fast_run_still_returns_200_with_the_whole_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unchanged contract, and the reason this ships without a UI lockstep.

    Most consults finish well inside the sync window. Those must return exactly
    what they returned before the run was detached — same status, same body —
    so a client that knows nothing about 202 keeps working.
    """
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    monkeypatch.setenv("LEGBA_CONSULT_SYNC_WAIT_SECONDS", "5")
    _install_dapr(monkeypatch)
    pg, nats = _Pg(), _Nats()

    with TestClient(_app(pg, nats)) as client:
        resp = _post(client)

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "the detached answer"
    assert body["status"] == "complete"
    assert body["session_id"] == "session-1"
    assert body["request_id"]
    assert pg.roles() == ["user", "assistant"]


def test_a_slow_run_returns_202_and_finishes_anyway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix, end to end: the POST lets go and the answer still lands.

    The 202 carries the ids the client needs and an empty answer. Nothing about
    the panel's behaviour changes at this boundary — its EventSource is already
    open, and the terminal frame below is where the answer arrives.
    """
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    monkeypatch.setenv("LEGBA_CONSULT_SYNC_WAIT_SECONDS", "0.1")
    _install_dapr(monkeypatch, delay=0.5)
    pg, nats = _Pg(), _Nats()

    with TestClient(_app(pg, nats)) as client:
        resp = _post(client, request_id="req-slow")
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "accepted"
        assert body["answer"] == ""
        assert body["request_id"] == "req-slow"
        assert body["session_id"] == "session-1"
        # At 202 time only the QUESTION is on record...
        assert pg.roles() == ["user"]
        # ...and the answer follows without anyone asking again.
        _wait(
            lambda: pg.roles() == ["user", "assistant"],
            what="the detached run to persist its answer",
        )

    assert pg.turn("assistant")["content"] == "the detached answer"


def test_the_terminal_frame_carries_the_answer_to_the_open_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actor's own final frame has no answer in it; this one does.

    A browser that only learned the run ENDED still had to fetch the text from
    the POST response — the dependency the 504 severed. The registry's frame
    closes that loop.
    """
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    monkeypatch.setenv("LEGBA_CONSULT_SYNC_WAIT_SECONDS", "0.1")
    _install_dapr(monkeypatch, delay=0.3)
    pg, nats = _Pg(), _Nats()

    with TestClient(_app(pg, nats)) as client:
        assert _post(client, request_id="req-frame").status_code == 202
        _wait(lambda: nats.nc.frames("final"), what="the terminal frame")

    frame = nats.nc.frames("final")[0]
    assert frame["final_source"] == "registry"
    assert frame["status"] == "complete"
    assert frame["response"]["answer"] == "the detached answer"
    assert frame["session_id"] == "session-1"


def test_the_run_status_endpoint_is_the_reconnect_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that lost its stream asks here rather than losing the turn."""
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    monkeypatch.setenv("LEGBA_CONSULT_SYNC_WAIT_SECONDS", "0.1")
    _install_dapr(monkeypatch, delay=0.4)
    pg, nats = _Pg(), _Nats()
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

    with TestClient(_app(pg, nats)) as client:
        assert _post(client, request_id="req-status").status_code == 202

        running = client.get("/api/v1/consult/runs/req-status", headers=headers)
        assert running.status_code == 200
        assert running.json()["status"] == "running"

        _wait(lambda: pg.roles() == ["user", "assistant"], what="the run to finish")
        done = client.get("/api/v1/consult/runs/req-status", headers=headers)
        assert done.json()["status"] == "complete"
        assert done.json()["response"]["answer"] == "the detached answer"

        unknown = client.get("/api/v1/consult/runs/nope", headers=headers)
        assert unknown.status_code == 404


# ---------------------------------------------------------------------------
# Failure — never a blank turn
# ---------------------------------------------------------------------------


def test_a_failure_inside_the_sync_window_still_raises_on_the_POST(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error contract for the fast path is exactly what it was."""
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    monkeypatch.setenv("LEGBA_CONSULT_SYNC_WAIT_SECONDS", "5")
    _install_dapr(
        monkeypatch, envelope={"outcome": "error", "error": "the actor said no"},
    )
    pg, nats = _Pg(), _Nats()

    with TestClient(_app(pg, nats), raise_server_exceptions=False) as client:
        resp = _post(client)

    assert resp.status_code == 502
    # ...and the failure still left a readable turn behind.
    assert pg.roles() == ["user", "assistant"]
    assert "did not finish" in pg.turn("assistant")["content"]


@pytest.mark.asyncio
async def test_a_failed_run_persists_the_partial_it_gathered() -> None:
    """The panel-never-blank contract, at the layer that has to honour it.

    The run watches its own step subject precisely so a failure has something
    to persist: the rounds that landed, the tools that ran, and the reason it
    stopped — as prose, not a status code.
    """
    run = ConsultRun(request_id="r1", session_id="s1")
    run.steps = [
        {"phase": "reason", "kind": "llm_call", "round": 1},
        {"phase": "act", "kind": "tool_call", "round": 1, "tool": "search_signals", "ok": True},
        {"phase": "act", "kind": "tool_call", "round": 2, "tool": "query_facts", "ok": False},
    ]
    run.error = (504, "the consult actor did not return in time")

    summary = run.partial_summary()
    assert "did not finish" in summary
    assert "2 round(s)" in summary
    assert "stopped at round 2" in summary
    assert "the consult actor did not return in time" in summary
    assert "`search_signals` — ok" in summary
    assert "`query_facts` — failed" in summary


@pytest.mark.asyncio
async def test_the_manager_accumulates_steps_off_the_runs_own_subject() -> None:
    """Steps are live core-NATS traffic with no replay — so the run listens too.

    Without this the registry would have nothing to persist when a run dies
    with the tab closed, and the partial above would be an empty gesture.
    """
    nats = _Nats()
    manager = ConsultRunManager(nats_store=nats)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _execute() -> dict[str, Any]:
        started.set()
        await release.wait()
        raise ConsultRunError(504, "ran out of time")

    run = manager.start(request_id="r2", session_id="s2", execute=_execute)
    await started.wait()
    await nats.nc.publish(
        "legba.consult.steps.r2",
        json.dumps({"type": "step", "kind": "tool_call", "round": 1}).encode(),
    )
    release.set()
    await run.task  # type: ignore[arg-type]

    assert run.status == "error"
    assert [s["kind"] for s in run.steps] == ["tool_call"]


@pytest.mark.asyncio
async def test_delivery_is_shielded_so_a_cancel_cannot_skip_the_write() -> None:
    """The exact gap the incident fell through, pinned.

    A cancel landing between the answer and the write would silently drop the
    turn. ``_deliver`` runs the hook under ``asyncio.shield``, so it completes
    even when the run task is cancelled mid-flight.
    """
    manager = ConsultRunManager()
    delivered: list[str] = []
    started = asyncio.Event()

    async def _execute() -> dict[str, Any]:
        started.set()
        await asyncio.sleep(10)  # cancelled here
        return {}

    async def _on_complete(run: ConsultRun) -> None:
        await asyncio.sleep(0)  # a real await, so a cancel could interleave
        delivered.append(run.status)

    run = manager.start(
        request_id="r3", session_id="s3", execute=_execute, on_complete=_on_complete,
    )
    await started.wait()
    run.task.cancel()  # type: ignore[union-attr]
    with pytest.raises(asyncio.CancelledError):
        await run.task  # type: ignore[arg-type]

    assert delivered == ["error"], "delivery was skipped by the cancellation"
    assert run.error[0] == 499


@pytest.mark.asyncio
async def test_a_terminal_frame_for_a_failure_carries_the_partial() -> None:
    """So a listening panel renders the partial instead of a bare error string."""
    nats = _Nats()
    run = ConsultRun(request_id="r4", session_id="s4", status="error")
    run.error = (503, "the Anthropic (Opus) plane is unavailable")
    run.steps = [{"kind": "tool_call", "round": 1, "tool": "search_signals", "ok": True}]

    await publish_terminal_frame(nats, request_id="r4", run=run)

    frame = nats.nc.frames("final")[0]
    assert frame["status"] == "error"
    assert frame["error_status"] == 503
    assert "did not finish" in frame["partial_answer"]
    assert frame["steps"][0]["tool"] == "search_signals"


@pytest.mark.asyncio
async def test_finished_runs_age_out_of_memory() -> None:
    """The status endpoint is a convenience, not a second database.

    The durable record is the persisted turn; keeping every run forever would
    be a slow leak on a busy registry.
    """
    manager = ConsultRunManager(retention_seconds=0.0, max_remembered=2)

    async def _ok() -> dict[str, Any]:
        return {"response": None, "steps": []}

    for i in range(3):
        run = manager.start(request_id=f"r{i}", session_id=None, execute=_ok)
        await run.task  # type: ignore[arg-type]

    await asyncio.sleep(0.01)
    assert manager.get("r0") is None
