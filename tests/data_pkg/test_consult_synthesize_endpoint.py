# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/consult/runs/{id}/synthesize`` and ``/stop`` — the front door's half.

The analyst-side proof that a replay sends the original prompt lives in
``test_consult_cost_and_synthesis``. What is tested HERE is the front door's
own job, which is mostly about not making things worse:

* it resolves the persisted turn — by run id, and by TURN id for the turns that
  predate migration 0195, which is every turn that existed when this was built
  (the c8a0105c turn among them, whose ``request_id`` is NULL);
* the invoke body carries ``synthesize_from`` and carries NOTHING that could
  let the analyst drill again — the run being recovered already spent its money
  on fifty tool calls and must not spend it twice;
* the recovered answer is a NEW turn linked to the old one, never an overwrite,
  so the cut answer and what it cost stay on the record;
* an un-recoverable turn is refused before the invoke, not after it;
* the default plane is the cheaper one.
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

API_TOKEN_ENV = "LEGBA_REGISTRY_API_TOKEN"
TEST_TOKEN = "fake-synthesize-token"

#: A persisted turn as it looks AFTER migration 0195 — carrying the run id and
#: the recorded synthesis prompt.
RECORDED_TRANSCRIPT: dict[str, Any] = {
    "version": 1,
    "usable": True,
    "truncated": False,
    "chars": 42,
    "system": "the original system prompt",
    "messages": [{"role": "user", "content": "the original user turn"}],
}

TRACE_STEPS: list[dict[str, Any]] = [
    {"phase": "act", "kind": "tool_call", "round": 1, "tool": "search_signals",
     "args": {"query": "iran"}, "result": {"count": 4, "refs": 2}, "ok": True},
]


class _Pg:
    """Enough of the turns table for the recovery read + the recovery write."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.inserted: list[dict[str, Any]] = []
        self.selects: list[tuple[str | None, str | None]] = []

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
                s = " ".join(sql.split())
                if "INSERT INTO consult_turns" in s:
                    pg.inserted.append({
                        "session_id": args[0], "role": args[1],
                        "content": args[2], "steps": args[3],
                        "request_id": args[7], "parent_turn_id": args[8],
                        "synthesis_status": args[9], "usage": args[11],
                    })
                    return {"id": f"turn-{len(pg.inserted)}"}
                if "FROM consult_turns" in s and "role = 'assistant'" in s:
                    pg.selects.append((args[0], args[1]))
                    if pg.row is None:
                        return None
                    # Mirror the SQL's own resolution rules so the test proves
                    # the ROUTE passes the right key, not merely that a fake
                    # returned something.
                    by_request = args[0] is not None and pg.row.get(
                        "request_id",
                    ) == args[0]
                    by_turn = args[1] is not None and pg.row["id"] == args[1]
                    return pg.row if (by_request or by_turn) else None
                raise AssertionError(f"unexpected SQL: {s[:90]!r}")

            async def fetchval(self, sql: str, *args: Any) -> Any:
                return "the original question"

            async def execute(self, sql: str, *args: Any) -> str:
                return "UPDATE 1"

        class _Ctx:
            async def __aenter__(self) -> Any:
                return _Conn()

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


class _DescRow:
    version = "v" + "c" * 16


class _Registry:
    def __init__(self, pg: Any) -> None:
        self.pg = pg

    async def get(self, descriptor_id: str, *, family: Any = None, version: Any = None) -> Any:
        return _DescRow()


class _StackRegistry:
    async def get(self, component_id: str) -> Any:
        return object()


RECOVERY_ENVELOPE: dict[str, Any] = {
    "outcome": "success",
    "mode": "chat",
    "derived_from": [],
    "consult_response": {
        "answer": "the answer the cut run never wrote",
        "uncertainty": 0.35,
        "unanswered_aspects": [],
        "cited_substrate_refs": [],
        "data": {
            "steps": TRACE_STEPS,
            "tool_calls": [],
            "replay_fidelity": "rebuilt",
            "replay_note": (
                "transcript rebuilt by re-executing 1 tool call(s) against the "
                "live corpus; assistant free text between rounds not "
                "recoverable (never persisted)"
            ),
            "synthesis_status": "complete",
            "usage": {"calls": 1, "input_tokens": 9000, "est_cost_usd": 0.21},
        },
    },
}


def _install_dapr(
    monkeypatch: pytest.MonkeyPatch,
    captured: list[dict[str, Any]],
    *,
    envelope: dict[str, Any] = RECOVERY_ENVELOPE,
) -> None:
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
            captured.append({"url": url, "body": json})
            return _Resp()

    monkeypatch.setattr(consult_api.httpx, "AsyncClient", _Client)


def _app(pg: _Pg) -> FastAPI:
    app = FastAPI()
    deps = RegistryAPIDeps(
        descriptor_registry=_Registry(pg),  # type: ignore[arg-type]
        stack_registry=_StackRegistry(),  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        dlq=None,  # type: ignore[arg-type]
        audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=None,  # type: ignore[arg-type]
        nats_store=None,
    )
    app.include_router(build_consult_router(deps), prefix="/api/v1")
    return app


def _router_run_manager(app: FastAPI) -> Any:
    """The ``ConsultRunManager`` the router built for itself.

    It is a closure local inside ``build_consult_router`` (one manager per
    app), so a test that wants the route to FIND a run has to reach the same
    instance. Fishing it out of the closure is ugly and deliberate: injecting a
    stand-in would leave the actual wiring — route to manager — untested.

    The walk has to DESCEND. Up to FastAPI 0.116 ``include_router`` copied each
    child ``APIRoute`` onto ``app.routes``, so one flat pass found the closure.
    From 0.140 it appends a single ``_IncludedRouter`` holding the original
    router instead, and the flat pass sees only the app's own ``/docs`` and
    ``/openapi.json`` — which is how this helper started raising its own
    assertion while every route it describes still worked. Recursing through
    anything that exposes ``routes`` / ``original_router`` keeps it true on
    both shapes, and on whatever the next version does.
    """
    from legba.data.registry import consult_runs

    def _walk(container: Any, seen: set[int]) -> Any:
        if id(container) in seen:
            return None
        seen.add(id(container))
        for route in getattr(container, "routes", ()) or ():
            closure = getattr(getattr(route, "endpoint", None), "__closure__", None)
            for cell in closure or ():
                try:
                    value = cell.cell_contents
                except ValueError:  # pragma: no cover — empty cell
                    continue
                if isinstance(value, consult_runs.ConsultRunManager):
                    return value
            for attr in ("original_router", "router", "app"):
                nested = getattr(route, attr, None)
                if nested is None or nested is route:
                    continue
                found = _walk(nested, seen)
                if found is not None:
                    return found
            found = _walk(route, seen)
            if found is not None:
                return found
        return None

    manager = _walk(app, set())
    if manager is not None:
        return manager
    raise AssertionError("the consult router must own a ConsultRunManager")


def _turn_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "id": "turn-original",
        "session_id": "session-1",
        "role": "assistant",
        "content": "**This consult ran out of its time budget…**",
        "steps": TRACE_STEPS,
        "tool_calls": [],
        "cited_refs": [{"id": "11111111-1111-1111-1111-111111111111"}],
        "finding_id": None,
        "request_id": "req-cut",
        "synthesis_status": "none",
        "replay_transcript": None,
        "usage": {"est_cost_usd": 9.80},
        "created_at": None,
    }
    row.update(overrides)
    return row


def _synthesize(client: TestClient, request_id: str = "req-cut", **body: Any) -> Any:
    return client.post(
        f"/api/v1/consult/runs/{request_id}/synthesize",
        json=body,
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )


# ---------------------------------------------------------------------------
# Resolving the turn
# ---------------------------------------------------------------------------


def test_a_cut_run_is_recovered_by_its_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    captured: list[dict[str, Any]] = []
    _install_dapr(monkeypatch, captured)
    pg = _Pg(_turn_row())

    with TestClient(_app(pg)) as client:
        resp = _synthesize(client)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer"] == "the answer the cut run never wrote"
    assert body["replay_fidelity"] == "rebuilt"
    assert "not recoverable" in body["replay_note"]
    assert body["parent_turn_id"] == "turn-original"


def test_the_operators_cut_turn_is_recovered_by_its_turn_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The c8a0105c turn's OWN shape, measured read-only from live Postgres.

    Session f12ebbd4-cac1-4692-ab10-1e045b48b2ae, turn
    ebee64e8-8b1a-4bb3-9900-bd6ec417ca89: 487 chars of apology, 62 steps (50
    ``tool_call``, 10 ``llm_call``, 1 ``render_prompt``, 1 ``degraded_final``),
    50 tool_calls, 283 cited_refs — and ``request_id`` NULL, because the column
    did not exist when it was written.

    That NULL is the point. Every turn persisted before migration 0195 has one,
    so a recovery that could only resolve by run id would be unable to recover
    the very run it was built for.
    """
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    captured: list[dict[str, Any]] = []
    _install_dapr(monkeypatch, captured)
    # 50 tool calls across 10 rounds, as recorded.
    steps = [
        {"phase": "act", "kind": "tool_call", "round": 1 + i // 5,
         "tool": "search_signals", "args": {"query": f"q{i}"},
         "result": {"count": 6, "refs": 5}, "ok": True}
        for i in range(50)
    ]
    steps.append({"phase": "reflect", "kind": "degraded_final",
                  "reason": "synthesis_deadline_exceeded"})
    pg = _Pg(_turn_row(
        id="ebee64e8-8b1a-4bb3-9900-bd6ec417ca89",
        session_id="f12ebbd4-cac1-4692-ab10-1e045b48b2ae",
        request_id=None,
        steps=steps,
        cited_refs=[{"id": f"{i:032x}"} for i in range(283)],
        replay_transcript=None,
    ))

    with TestClient(_app(pg)) as client:
        # By run id it cannot be found...
        missing = _synthesize(client, "c8a0105c")
        assert missing.status_code == 404
        # ...by turn id it can.
        resp = _synthesize(
            client, "c8a0105c", turn_id="ebee64e8-8b1a-4bb3-9900-bd6ec417ca89",
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["parent_turn_id"] == "ebee64e8-8b1a-4bb3-9900-bd6ec417ca89"
    # It takes the REBUILD path: no recorded transcript exists for it.
    sent = captured[0]["body"]["inputs"][0]["synthesize_from"]
    assert sent["replay_transcript"] is None
    assert len(sent["steps"]) == 51
    assert len(sent["cited_refs"]) == 283


def test_a_turn_with_no_evidence_is_refused_before_the_invoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No transcript, no tool calls ⇒ 404 and NO actor invoke.

    Checked at the front door so an un-recoverable turn costs nothing at all —
    not a round trip, not a model call.
    """
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    captured: list[dict[str, Any]] = []
    _install_dapr(monkeypatch, captured)
    pg = _Pg(_turn_row(steps=[], replay_transcript=None))

    with TestClient(_app(pg)) as client:
        resp = _synthesize(client)

    assert resp.status_code == 404
    assert "no evidence to synthesise over" in resp.json()["detail"]
    assert captured == [], "an un-recoverable turn must never reach the actor"


def test_an_unknown_run_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    _install_dapr(monkeypatch, [])
    pg = _Pg(None)

    with TestClient(_app(pg)) as client:
        resp = _synthesize(client)

    assert resp.status_code == 404
    assert "no persisted assistant turn" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# The invoke body — what the recovery is NOT allowed to do
# ---------------------------------------------------------------------------


def test_the_recovery_invoke_carries_evidence_and_no_way_to_drill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one thing this endpoint must never become is a second expensive run."""
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    captured: list[dict[str, Any]] = []
    _install_dapr(monkeypatch, captured)
    pg = _Pg(_turn_row(replay_transcript=RECORDED_TRANSCRIPT))

    with TestClient(_app(pg)) as client:
        assert _synthesize(client).status_code == 200

    assert len(captured) == 1
    first_input = captured[0]["body"]["inputs"][0]
    assert "synthesize_from" in first_input
    assert first_input["synthesize_from"]["replay_transcript"] == RECORDED_TRANSCRIPT
    assert first_input["synthesize_from"]["steps"] == TRACE_STEPS
    # cited_refs are flattened from the projected {id: ...} objects.
    assert first_input["synthesize_from"]["cited_refs"] == [
        "11111111-1111-1111-1111-111111111111",
    ]
    # NOTHING that could drive a new ReAct loop.
    assert "max_tool_rounds" not in first_input
    assert "messages" not in first_input
    assert "pinned_context" not in first_input


def test_the_default_plane_is_the_cheaper_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recovery is ONE call over existing evidence; it must not inherit the
    expensive route that failed to finish the first time."""
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    captured: list[dict[str, Any]] = []
    _install_dapr(monkeypatch, captured)
    pg = _Pg(_turn_row())

    with TestClient(_app(pg)) as client:
        resp = _synthesize(client)

    assert resp.json()["model"] == "opus"
    # "opus" IS the default plane, so no override is threaded — the run keeps
    # the cached ACTIVATE-time primary, matching the chat path's contract.
    assert "llm_component_override" not in captured[0]["body"]["inputs"][0]


def test_an_explicit_plane_is_threaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    captured: list[dict[str, Any]] = []
    _install_dapr(monkeypatch, captured)
    pg = _Pg(_turn_row())

    with TestClient(_app(pg)) as client:
        resp = _synthesize(client, model="core")

    assert resp.json()["model"] == "core"
    assert captured[0]["body"]["inputs"][0]["llm_component_override"] == (
        "llm.primary.openai_compat"
    )


# ---------------------------------------------------------------------------
# The new turn
# ---------------------------------------------------------------------------


def test_the_recovered_answer_is_a_new_turn_linked_to_the_old_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never an overwrite. The cut answer and what it cost stay on the record —
    otherwise the next post-mortem cannot see that this happened at all."""
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    _install_dapr(monkeypatch, [])
    pg = _Pg(_turn_row())

    with TestClient(_app(pg)) as client:
        resp = _synthesize(client)

    assert resp.status_code == 200
    assert len(pg.inserted) == 1
    written = pg.inserted[0]
    assert written["role"] == "assistant"
    assert written["content"] == "the answer the cut run never wrote"
    assert written["parent_turn_id"] == "turn-original"
    assert written["synthesis_status"] == "complete"
    assert written["request_id"] and written["request_id"] != "req-cut", (
        "the recovery is its own run and gets its own id"
    )
    assert json.loads(written["usage"])["est_cost_usd"] == 0.21


# ---------------------------------------------------------------------------
# STOP
# ---------------------------------------------------------------------------


def test_stopping_an_unknown_run_is_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    pg = _Pg(None)

    with TestClient(_app(pg)) as client:
        resp = client.post(
            "/api/v1/consult/runs/nope/stop",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )

    assert resp.status_code == 404


def test_stop_cancels_the_run_and_waits_for_its_partial_to_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before this endpoint there was no way to stop a run at all.

    "Dismiss" only detached the browser; the run kept drilling and kept
    billing. Stopping must cancel the task AND wait for the cancelled run to
    persist its partial turn, because the caller's very next move is
    ``/synthesize`` over exactly that turn.
    """
    monkeypatch.setenv(API_TOKEN_ENV, TEST_TOKEN)
    monkeypatch.setenv("LEGBA_CONSULT_SYNC_WAIT_SECONDS", "0.05")
    _install_dapr(monkeypatch, [])
    pg = _Pg(_turn_row())

    app = _app(pg)
    persisted = asyncio.Event()

    async def _forever() -> dict[str, Any]:
        await asyncio.sleep(60)
        raise AssertionError("unreachable — the run must be cancelled")

    async def _on_complete(run: Any) -> None:
        persisted.set()

    with TestClient(app) as client:
        # Start a REAL run on the router's OWN manager, so the route has to
        # find it the way it will in production. A stand-in would prove
        # nothing about the wiring.
        manager = _router_run_manager(app)
        client.portal.call(
            lambda: manager.start(
                request_id="req-live",
                session_id="session-1",
                execute=_forever,
                on_complete=_on_complete,
            ),
        )
        assert manager.get("req-live").status == "running"

        resp = client.post(
            "/api/v1/consult/runs/req-live/stop",
            headers={"Authorization": f"Bearer {TEST_TOKEN}"},
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "stopped"
    assert persisted.is_set(), (
        "stop must not return before the cancelled run's turn is delivered — "
        "the caller's next move is /synthesize over exactly that turn"
    )
