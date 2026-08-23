# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Does a client disconnect mid-consult lose the assistant turn? (GLASS-4)

Why this test exists
====================

The consult panel's partial-turn persistence (the zustand store +
``localStorage`` reconcile in ``legba-ui-v3/src/state/consultSession.ts``)
rests on ONE backend assumption: when the browser goes away mid-turn — a
reload, a closed tab, a killed connection — the registry still finishes the
run and still appends the assistant turn to ``consult_turns``.  The client's
reconcile-on-remount reads that row back through
``GET /api/v1/consult/sessions/{id}`` and only keeps its local ``pendingTurn``
when the server has NOT recorded an answer yet.  If the write were lost on
disconnect, that reconcile would wait forever for a row that is never coming,
and the panel would show a permanently-stuck "Consulting…".

So the assumption is correctness-critical and is proven here rather than
assumed.  ``POST /api/v1/consult`` is a plain request/response handler:
``consult_api.invoke_consult`` appends the USER turn before the actor runs
and calls ``_persist_assistant_turn`` after the Dapr invoke returns.  Both
writes sit inside the request handler's own lifetime, so "does a disconnect
kill the handler?" is exactly "does the answer survive?".

The verdict (see :func:`test_assistant_turn_survives_client_disconnect`)
=======================================================================

**The write survives.**  Neither layer that could cancel the handler does:

  * ``starlette.routing.request_response`` (starlette 1.0.0) awaits the
    endpoint directly — there is no task group racing the handler against an
    ``http.disconnect`` message, so starlette never cancels it.
  * uvicorn's ``connection_lost`` (both the httptools and h11 protocol
    implementations) only sets ``cycle.disconnected = True`` and wakes
    ``message_event``; it never cancels the ``run_asgi`` task.  A disconnected
    cycle simply discards the response bytes it can no longer write.

Reading that source is not proof, though — it is a claim about two pinned
third-party packages that a version bump could silently invalidate.  This
test pins the BEHAVIOUR through the real stack: a real uvicorn on a real
socket, the real ``build_consult_router`` handler, and a real TCP abort
(``SO_LINGER {1, 0}`` → RST) issued while the handler is parked inside the
Dapr invoke.  The assistant turn must still land.

Because "the write survived" is also what a test that never actually
disconnected would report, the disconnect itself is proven independently:
:func:`test_control_probe_proves_the_server_observes_the_disconnect` drives
the SAME abort against a probe route that polls ``request.is_disconnected()``
and asserts the server observed it.  Together the two say what one alone
cannot — the client really did vanish, and the handler really did finish
anyway.

Consequence for the panel: the assistant turn is durable across a disconnect,
so ``_persist_assistant_turn`` is deliberately left ON the request lifecycle.
It does NOT need the detached shape ``deep_consult_api`` uses (a background
workflow whose completion is recorded by the status poll via
``consult_persistence.record_deep_completion``) — that shape exists there
because a deep consult outlives ANY request by design, not because a
disconnect would lose it.

What is NOT covered (documented gaps, not silent ones)
======================================================

  * A registry process that dies mid-run loses the in-flight assistant turn.
    Nothing in-process can survive SIGKILL; the user turn is already durable
    (it is appended before the actor runs), so the question is never lost.
  * A FIRST turn interrupted before the POST response arrives leaves a
    session the client never learned the id of.  The row is written and the
    history sidebar lists it, but automatic reattach-by-id is impossible —
    the server mints ``session_id`` and the client's ``request_id`` is not
    stored on ``consult_sessions``.
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import anyio
import pytest
import uvicorn
from fastapi import APIRouter, FastAPI, Request
from starlette.types import ASGIApp, Receive, Scope, Send

import legba.data.registry.consult_api as consult_api
from legba.data.registry.api import RegistryAPIDeps
from legba.data.registry.consult_api import build_consult_router

API_TOKEN_ENV = "LEGBA_REGISTRY_API_TOKEN"
TEST_TOKEN = "disconnect-test-token"

#: How long a stub is willing to stay parked before it gives up, so a broken
#: test fails loudly instead of hanging the suite.
_STUB_MAX_PARK_SECONDS = 20.0


# ---------------------------------------------------------------------------
# Fakes — a recording pg pool + a Dapr sidecar we can park mid-invoke
# ---------------------------------------------------------------------------


class _RecordingPg:
    """An asyncpg-pool stand-in that records the audit writes it is handed.

    Only the three statements ``consult_persistence`` issues are understood
    (session insert, turn insert, session touch); anything else raises so a
    future query cannot pass this test by silently returning ``None``.
    """

    def __init__(self) -> None:
        self.session_ids: list[str] = []
        self.turns: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def roles(self) -> list[str]:
        with self._lock:
            return [t["role"] for t in self.turns]

    def turn(self, role: str) -> dict[str, Any] | None:
        with self._lock:
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
                    session_id = f"session-{len(pg.session_ids)}"
                    with pg._lock:
                        pg.session_ids.append(session_id)
                    return {"id": session_id}
                if "INSERT INTO consult_turns" in sql:
                    with pg._lock:
                        pg.turns.append(
                            {
                                "session_id": args[0],
                                "role": args[1],
                                "content": args[2],
                                "steps": args[3],
                                "tool_calls": args[4],
                                "cited_refs": args[5],
                                "finding_id": args[6],
                            }
                        )
                        return {"id": f"turn-{len(pg.turns)}"}
                raise AssertionError(f"unexpected fetchrow SQL: {sql[:120]!r}")

            async def execute(self, sql: str, *args: Any) -> str:
                if "UPDATE consult_sessions" in sql:
                    return "UPDATE 1"
                raise AssertionError(f"unexpected execute SQL: {sql[:120]!r}")

        class _Ctx:
            async def __aenter__(self) -> Any:
                return _Conn()

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


class _DescRow:
    version = "v" + "a" * 16


class _DescriptorRegistry:
    def __init__(self, pg: Any) -> None:
        self.pg = pg

    async def get(self, descriptor_id: str, *, family: Any, version: Any = None) -> Any:
        return _DescRow()


class _Gate:
    """A cross-thread park/release pair the stubs and the test both hold.

    ``reached`` is set by the server thread the moment the handler enters the
    Dapr invoke — i.e. after the USER turn is already persisted and before the
    ASSISTANT turn could be.  That is precisely the window a browser reload
    lands in, so the test aborts the socket there.
    """

    def __init__(self) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()

    async def park(self) -> None:
        self.reached.set()
        deadline = time.monotonic() + _STUB_MAX_PARK_SECONDS
        while not self.release.is_set():
            if time.monotonic() > deadline:
                raise AssertionError("gate never released — the test is wedged")
            await asyncio.sleep(0.01)


def _install_gated_dapr(
    monkeypatch: pytest.MonkeyPatch, gate: _Gate, envelope: dict[str, Any]
) -> None:
    """Replace ``consult_api.httpx.AsyncClient`` with a sidecar that parks.

    The real actor invoke is the long pole of a consult; parking here lets the
    test disconnect the client at a deterministic point mid-run instead of
    racing a sleep.
    """

    class _Resp:
        status_code = 200

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
            await gate.park()
            return _Resp()

    monkeypatch.setattr(consult_api.httpx, "AsyncClient", _Client)


CHAT_ENVELOPE: dict[str, Any] = {
    "outcome": "success",
    "mode": "chat",
    "derived_from": [],
    "consult_response": {
        "answer": "the answer that must survive the disconnect",
        "uncertainty": 0.2,
        "unanswered_aspects": [],
        "cited_substrate_refs": [],
        "data": {"steps": [{"type": "step", "phase": "act"}], "tool_calls": []},
    },
}


# ---------------------------------------------------------------------------
# The app under test — the REAL consult router, mounted the way server.py
# mounts it, plus a disconnect-observing control probe.
# ---------------------------------------------------------------------------


def _build_app(pg: _RecordingPg, probe_gate: _Gate, observed: dict[str, Any]) -> FastAPI:
    app = FastAPI()
    deps = RegistryAPIDeps(
        descriptor_registry=_DescriptorRegistry(pg),  # type: ignore[arg-type]
        stack_registry=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        dlq=None,  # type: ignore[arg-type]
        audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=None,  # type: ignore[arg-type]
        nats_store=None,
    )
    app.include_router(build_consult_router(deps), prefix="/api/v1")

    probe = APIRouter()

    @probe.post("/probe/disconnect")
    async def probe_disconnect(request: Request) -> dict[str, bool]:
        """Control route: park like the consult handler, but WATCH the client.

        Proves the abort the test issues is a real disconnect the server can
        see. Without this, "the assistant turn was written" would also be the
        result of a test whose client never actually went away.
        """
        probe_gate.reached.set()
        deadline = time.monotonic() + _STUB_MAX_PARK_SECONDS
        while time.monotonic() < deadline:
            if await request.is_disconnected():
                observed["disconnected"] = True
                break
            await asyncio.sleep(0.01)
        observed["completed"] = True
        return {"ok": True}

    app.include_router(probe)
    return app


class _CancelOnDisconnect:
    """A middleware that DOES cancel the handler when the client disconnects.

    This is the counterfactual, and it is what gives the verdict its teeth: a
    test asserting "the write survived" is worthless unless a world where the
    write is lost would actually fail it.  Mounting the SAME consult router
    under this middleware produces exactly that world — the shape starlette
    would have if ``request_response`` raced the endpoint against
    ``http.disconnect`` — and :func:`test_the_write_would_be_lost_if_the_handler_were_cancelled`
    shows the assistant turn then goes missing.

    The request body is drained up front so the disconnect watcher can own
    ``receive`` while the downstream app is served a replay of the body it
    would otherwise have consumed.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if not message.get("more_body"):
                break

        replayed = False

        async def replay_receive() -> Any:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            await anyio.sleep_forever()
            return None  # unreachable — sleep_forever never returns; RET503 wants it said

        async with anyio.create_task_group() as task_group:

            async def watch_disconnect() -> None:
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        task_group.cancel_scope.cancel()
                        return

            async def run_app() -> None:
                await self.app(scope, replay_receive, send)
                task_group.cancel_scope.cancel()

            task_group.start_soon(watch_disconnect)
            task_group.start_soon(run_app)


class _UvicornThread:
    """A real uvicorn on a real localhost port, run on its own event loop.

    Mirrors the harness in ``tests/runtime/test_a2a_skill_router_e2e.py`` —
    an in-process ASGI transport would not exercise the socket teardown this
    test is entirely about.
    """

    def __init__(self, app: ASGIApp, port: int) -> None:
        self._port = port
        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="warning",
                log_config=None,
                lifespan="off",
            )
        )
        self._thread: threading.Thread | None = None

    def start(self, *, timeout_s: float = 5.0) -> None:
        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._server.serve())
            finally:
                with suppress(Exception):
                    loop.close()

        self._thread = threading.Thread(
            target=_run, name="consult-disconnect-uvicorn", daemon=True
        )
        self._thread.start()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._server.started:
                return
            time.sleep(0.02)
        raise RuntimeError(f"uvicorn did not start within {timeout_s}s on :{self._port}")

    def stop(self, *, timeout_s: float = 5.0) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _send_post(port: int, path: str, payload: dict[str, Any]) -> socket.socket:
    """Open a raw socket and write one complete POST; return the live socket.

    Raw rather than ``httpx``/``requests`` because the whole point is to
    control HOW the connection ends — a client library closes it for us, and
    not necessarily with a RST.
    """
    body = json.dumps(payload).encode("utf-8")
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Authorization: Bearer {TEST_TOKEN}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    ).encode("utf-8") + body
    sock = socket.create_connection(("127.0.0.1", port), timeout=5.0)
    sock.sendall(request)
    return sock


def _abort(sock: socket.socket, *, how: str) -> None:
    """End the connection the way a vanished browser does.

    ``rst`` — ``SO_LINGER {1, 0}`` makes ``close()`` emit a RST, the harshest
    teardown and what a killed tab/process produces.  ``fin`` — an ordinary
    close, the orderly shutdown a navigating browser produces.  Both are
    exercised: they take different paths through uvicorn's protocol
    (``connection_lost`` vs ``eof_received``).
    """
    if how == "rst":
        # linger ON with a zero timeout ⇒ close() sends RST instead of FIN.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()


def _wait_for(predicate: Any, *, timeout_s: float = 10.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")


@pytest.fixture()
def consult_server(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The real consult router on a real uvicorn, with a parkable sidecar."""
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    pg = _RecordingPg()
    gate = _Gate()
    probe_gate = _Gate()
    observed: dict[str, Any] = {}
    _install_gated_dapr(monkeypatch, gate, CHAT_ENVELOPE)

    port = _free_port()
    server = _UvicornThread(_build_app(pg, probe_gate, observed), port)
    server.start()
    try:
        yield {
            "port": port,
            "pg": pg,
            "gate": gate,
            "probe_gate": probe_gate,
            "observed": observed,
        }
    finally:
        gate.release.set()
        probe_gate.release.set()
        server.stop()


@pytest.fixture()
def cancelling_consult_server(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The same router, wrapped so a disconnect DOES cancel the handler."""
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    pg = _RecordingPg()
    gate = _Gate()
    _install_gated_dapr(monkeypatch, gate, CHAT_ENVELOPE)

    port = _free_port()
    app = _build_app(pg, _Gate(), {})
    server = _UvicornThread(_CancelOnDisconnect(app), port)  # type: ignore[arg-type]
    server.start()
    try:
        yield {"port": port, "pg": pg, "gate": gate}
    finally:
        gate.release.set()
        server.stop()


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["rst", "fin"])
def test_assistant_turn_survives_client_disconnect(consult_server: Any, how: str) -> None:
    """A browser that vanishes mid-consult still gets its answer persisted.

    The sequence is the exact shape of a reload during an in-flight turn:
    POST, wait until the handler is inside the actor invoke (user turn already
    on record, assistant turn not yet), kill the socket, then let the actor
    finish.  ``_persist_assistant_turn`` must still run.

    This is the invariant the UI's reconcile-on-remount depends on: server
    truth eventually carries the answer, so a local ``pendingTurn`` can be
    dropped the moment the server records one.
    """
    port = consult_server["port"]
    pg: _RecordingPg = consult_server["pg"]
    gate: _Gate = consult_server["gate"]

    sock = _send_post(
        port,
        "/api/v1/consult",
        {
            "question": "what survives a disconnect?",
            "mode": "chat",
            "messages": [],
        },
    )

    # Park point: the handler is inside the Dapr invoke.
    _wait_for(gate.reached.is_set, what="the handler to reach the actor invoke")
    assert pg.roles() == ["user"], (
        "precondition: the user turn is appended BEFORE the actor runs, and the "
        f"assistant turn not yet — got {pg.roles()}"
    )

    # The browser goes away.
    _abort(sock, how=how)
    time.sleep(0.2)  # let the server's protocol notice the teardown

    # The actor finishes anyway.
    gate.release.set()

    _wait_for(
        lambda: "assistant" in pg.roles(),
        what="the assistant turn to be persisted after the client disconnected",
    )

    assistant = pg.turn("assistant")
    assert assistant is not None
    assert assistant["content"] == "the answer that must survive the disconnect"
    assert assistant["session_id"] == pg.session_ids[0]
    # The ReAct trace rides along — a reattaching client re-seeds the full turn,
    # not a bare answer string.
    assert json.loads(assistant["steps"]) == [{"type": "step", "phase": "act"}]


def test_control_probe_proves_the_server_observes_the_disconnect(
    consult_server: Any,
) -> None:
    """The abort is real — a watching handler sees ``http.disconnect``.

    This is the negative control for the test above.  ``request.is_disconnected()``
    returning True proves the server-side connection genuinely went away, so
    "the assistant turn was written" cannot be explained by a client that
    never actually disconnected.
    """
    port = consult_server["port"]
    probe_gate: _Gate = consult_server["probe_gate"]
    observed: dict[str, Any] = consult_server["observed"]

    sock = _send_post(port, "/probe/disconnect", {"x": 1})
    _wait_for(probe_gate.reached.is_set, what="the probe handler to start")

    _abort(sock, how="rst")

    _wait_for(
        lambda: observed.get("disconnected") is True,
        what="the server to observe http.disconnect",
    )
    # And the handler still ran to completion after seeing it — starlette did
    # not cancel it out from under us.
    _wait_for(
        lambda: observed.get("completed") is True,
        what="the probe handler to finish after the disconnect",
    )


def test_the_write_would_be_lost_if_the_handler_were_cancelled(
    cancelling_consult_server: Any,
) -> None:
    """The counterfactual: under cancellation, the answer IS lost.

    Same router, same abort, same actor — the only difference is a middleware
    that cancels the handler on ``http.disconnect``.  The user turn (written
    before the actor runs) survives; the assistant turn never happens.

    This is what makes the passing verdict meaningful rather than vacuous: the
    harness demonstrably detects a lost write, so
    :func:`test_assistant_turn_survives_client_disconnect` passing says
    something about the production stack instead of about the test.
    """
    port = cancelling_consult_server["port"]
    pg: _RecordingPg = cancelling_consult_server["pg"]
    gate: _Gate = cancelling_consult_server["gate"]

    sock = _send_post(
        port,
        "/api/v1/consult",
        {"question": "what if the handler were cancelled?", "mode": "chat", "messages": []},
    )
    _wait_for(gate.reached.is_set, what="the handler to reach the actor invoke")
    assert pg.roles() == ["user"]

    _abort(sock, how="rst")
    time.sleep(0.3)  # let the cancellation propagate
    gate.release.set()
    time.sleep(0.5)  # give a surviving handler every chance to write

    assert pg.roles() == ["user"], (
        "the cancelling middleware was supposed to drop the assistant turn — if "
        "it landed anyway this counterfactual no longer proves anything, and the "
        "survival test above needs a new negative control"
    )


def test_the_router_under_test_comes_from_this_checkout() -> None:
    """Guard the verdict against testing somebody else's copy of the router.

    An editable install puts ``.../legba/src`` on ``sys.path`` via a ``.pth``
    file pointing at the MAIN checkout. In a git worktree that silently wins:
    ``import legba`` resolves to the main checkout's source, so a test run
    without ``PYTHONPATH=src`` exercises code that is not the code under
    review — and reports a confident verdict about it.

    Nothing in the failure is visible: the imports succeed, the tests pass, and
    the result describes the wrong tree. So this asserts the module actually
    loaded lives beside this test, turning a silent misattribution into a loud
    one. Run this suite as ``PYTHONPATH=src pytest tests/data_pkg/...``.
    """
    repo_root = Path(__file__).resolve().parents[2]
    module_path = Path(consult_api.__file__).resolve()
    assert repo_root in module_path.parents, (
        f"consult_api was imported from {module_path}, which is OUTSIDE this "
        f"checkout ({repo_root}) — an editable-install .pth is shadowing the "
        f"local source. Re-run with PYTHONPATH=src; the verdict this module "
        f"reports is about whichever tree it actually imported."
    )


def test_dependency_versions_that_the_verdict_rests_on() -> None:
    """Fail loudly if the two packages whose behaviour was pinned change major.

    The verdict above is a property of starlette's ``request_response`` (no
    disconnect race) and uvicorn's ``connection_lost`` (no task cancel).  The
    behaviour tests are the real guard, but this makes a MAJOR bump of either
    package an explicit prompt to re-read them rather than a silent inherit.
    """
    import starlette

    assert starlette.__version__.split(".")[0] == "1", (
        f"starlette major changed ({starlette.__version__}) — re-verify that "
        "request_response still does not cancel the handler on client disconnect"
    )
    assert uvicorn.__version__.split(".")[0] == "0", (
        f"uvicorn major changed ({uvicorn.__version__}) — re-verify that "
        "connection_lost still does not cancel the run_asgi task"
    )


def test_persistence_is_deliberately_on_the_request_lifecycle() -> None:
    """Pin the DECISION, so a future reader sees it was made, not overlooked.

    ``consult_api._persist_assistant_turn`` is awaited inline in the request
    handler.  ``deep_consult_api`` uses the detached shape instead
    (``consult_persistence.record_deep_completion``, driven by the status
    poll) because a deep consult outlives any request BY DESIGN — not because
    a disconnect would lose the write.  The tests above prove the chat path
    does not need that shape; this asserts both shapes still exist as
    described so the comparison in the module docstring stays true.
    """
    import inspect

    from legba.data.registry import consult_persistence, deep_consult_api

    chat_src = inspect.getsource(consult_api.build_consult_router)
    assert "await _persist_assistant_turn(" in chat_src, (
        "the chat path no longer persists inline — if it was detached, update "
        "this module's verdict and the UI reconcile that depends on it"
    )
    assert not any(
        marker in chat_src
        for marker in ("create_task(", "BackgroundTask", "background_tasks")
    ), "the chat path acquired a detached write — re-derive the verdict"

    deep_src = inspect.getsource(deep_consult_api)
    assert "record_deep_completion" in deep_src
    assert hasattr(consult_persistence, "record_deep_completion")
