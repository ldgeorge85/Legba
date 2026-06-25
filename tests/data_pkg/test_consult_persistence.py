# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Consult audit-trail persistence + read-route tests (0038).

Covers:
  * consult_persistence.create_session / append_turn / list_sessions /
    load_session against an in-memory fake pg that models the 0038 tables.
  * record_deep_completion is idempotent (at-most-once assistant turn).
  * Best-effort discipline: a DB error in a write helper returns None, never
    raises (the consult request must not fail on an audit outage).
  * The read routes GET /consult/sessions (list) + /consult/sessions/{id}
    (load + 404) return the projected shapes.

The fake pg implements just enough SQL semantics (recognised by substring) to
exercise the helpers without a live Postgres — the in-container suite's live
acceptance is covered separately by the chat-path integration test.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from legba.data.registry import consult_persistence
from legba.data.registry.api import RegistryAPIDeps
from legba.data.registry.consult_sessions_api import build_consult_sessions_router


API_TOKEN_ENV = "LEGBA_REGISTRY_API_TOKEN"


# ---------------------------------------------------------------------------
# In-memory fake pg modelling the 0038 tables
# ---------------------------------------------------------------------------


class _FakePg:
    """Models consult_sessions + consult_turns with the minimal SQL the
    persistence helpers issue. Queries are matched by substring (the helpers
    use fixed SQL), so this is a faithful-enough stand-in for unit coverage."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.turns: list[dict[str, Any]] = []
        self.fail = False  # flip to simulate a DB outage

    def acquire(self):
        outer = self

        class _Ctx:
            async def __aenter__(self_inner):
                return _Conn(outer)

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


class _Conn:
    def __init__(self, pg: _FakePg) -> None:
        self.pg = pg

    def transaction(self):
        class _Txn:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _Txn()

    async def fetchrow(self, sql: str, *args):
        if self.pg.fail:
            raise RuntimeError("simulated DB outage")
        s = " ".join(sql.split())
        if "INSERT INTO consult_sessions" in s:
            mode, title, principal, task_id, run_id = args
            sid = str(uuid4())
            now = datetime.now(timezone.utc)
            self.pg.sessions[sid] = {
                "id": sid, "mode": mode, "title": title, "principal": principal,
                "task_id": task_id, "run_id": run_id,
                "created_at": now, "updated_at": now,
            }
            return {"id": sid}
        if "INSERT INTO consult_turns" in s:
            (session_id, role, content, steps, tool_calls, cited_refs,
             finding_id) = args
            tid = str(uuid4())
            self.pg.turns.append({
                "id": tid, "session_id": session_id, "role": role,
                "content": content, "steps": steps, "tool_calls": tool_calls,
                "cited_refs": cited_refs, "finding_id": finding_id,
                "created_at": datetime.now(timezone.utc),
            })
            return {"id": tid}
        if "FROM consult_sessions" in s and "WHERE task_id" in s:
            task_id = args[0]
            matches = [
                v for v in self.pg.sessions.values() if v["task_id"] == task_id
            ]
            matches.sort(key=lambda v: v["created_at"], reverse=True)
            return {"id": matches[0]["id"]} if matches else None
        if "FROM consult_sessions" in s and "WHERE id" in s:
            sid = args[0]
            return self.pg.sessions.get(sid)
        raise AssertionError(f"unexpected fetchrow: {s}")

    async def fetchval(self, sql: str, *args):
        if self.pg.fail:
            raise RuntimeError("simulated DB outage")
        s = " ".join(sql.split())
        if "FROM consult_turns" in s and "role = 'assistant'" in s:
            sid = args[0]
            for t in self.pg.turns:
                if t["session_id"] == sid and t["role"] == "assistant":
                    return 1
            return None
        raise AssertionError(f"unexpected fetchval: {s}")

    async def execute(self, sql: str, *args):
        if self.pg.fail:
            raise RuntimeError("simulated DB outage")
        s = " ".join(sql.split())
        if "UPDATE consult_sessions SET updated_at" in s:
            sid = args[0]
            if sid in self.pg.sessions:
                self.pg.sessions[sid]["updated_at"] = datetime.now(timezone.utc)
            return "UPDATE 1"
        raise AssertionError(f"unexpected execute: {s}")

    async def fetch(self, sql: str, *args):
        if self.pg.fail:
            raise RuntimeError("simulated DB outage")
        s = " ".join(sql.split())
        if "FROM consult_sessions" in s:
            rows = list(self.pg.sessions.values())
            # optional mode filter is arg index 1 (limit is arg 0)
            if len(args) > 1:
                rows = [r for r in rows if r["mode"] == args[1]]
            rows.sort(key=lambda v: v["updated_at"], reverse=True)
            rows = rows[: args[0]]
            out = []
            for r in rows:
                tc = sum(1 for t in self.pg.turns if t["session_id"] == r["id"])
                out.append({**r, "turn_count": tc})
            return out
        if "FROM consult_turns" in s and "WHERE session_id" in s:
            sid = args[0]
            rows = [t for t in self.pg.turns if t["session_id"] == sid]
            rows.sort(key=lambda v: v["created_at"])
            return rows
        raise AssertionError(f"unexpected fetch: {s}")


# ---------------------------------------------------------------------------
# Persistence-helper unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_append_load_roundtrip():
    pg = _FakePg()
    sid = await consult_persistence.create_session(
        pg, mode="chat", question="What touches Brazil?", principal="op",
    )
    assert sid
    await consult_persistence.append_turn(
        pg, session_id=sid, role="user", content="What touches Brazil?",
    )
    await consult_persistence.append_turn(
        pg, session_id=sid, role="assistant", content="Two signals.",
        tool_calls=[{"tool": "search_signals", "args": {}}],
        cited_refs=[{"kind": "signal", "id": "abc"}],
    )

    loaded = await consult_persistence.load_session(pg, sid)
    assert loaded is not None
    assert loaded["mode"] == "chat"
    assert loaded["title"].startswith("What touches Brazil")
    assert [t["role"] for t in loaded["turns"]] == ["user", "assistant"]
    assistant = loaded["turns"][1]
    assert assistant["tool_calls"][0]["tool"] == "search_signals"
    assert assistant["cited_refs"][0]["id"] == "abc"


@pytest.mark.asyncio
async def test_list_orders_recent_first_and_filters_mode():
    pg = _FakePg()
    chat = await consult_persistence.create_session(
        pg, mode="chat", question="q1",
    )
    deep = await consult_persistence.create_session(
        pg, mode="deep", question="q2", task_id="t-1", run_id="r-1",
    )
    await consult_persistence.append_turn(pg, session_id=deep, role="user", content="q2")

    all_rows = await consult_persistence.list_sessions(pg)
    ids = {r["id"] for r in all_rows}
    assert chat in ids and deep in ids

    deep_only = await consult_persistence.list_sessions(pg, mode="deep")
    assert [r["id"] for r in deep_only] == [deep]
    assert deep_only[0]["task_id"] == "t-1"
    assert deep_only[0]["turn_count"] == 1


@pytest.mark.asyncio
async def test_record_deep_completion_is_idempotent():
    pg = _FakePg()
    sid = await consult_persistence.create_session(
        pg, mode="deep", question="q", task_id="task-xyz", run_id="run-xyz",
    )
    await consult_persistence.append_turn(pg, session_id=sid, role="user", content="q")

    first = await consult_persistence.record_deep_completion(
        pg, task_id="task-xyz", answer="done", finding_id="f-1",
    )
    assert first  # newly written
    second = await consult_persistence.record_deep_completion(
        pg, task_id="task-xyz", answer="done again", finding_id="f-1",
    )
    assert second is None  # at-most-once

    loaded = await consult_persistence.load_session(pg, sid)
    assert [t["role"] for t in loaded["turns"]] == ["user", "assistant"]
    assert loaded["turns"][1]["content"] == "done"


@pytest.mark.asyncio
async def test_write_helpers_swallow_db_errors():
    pg = _FakePg()
    pg.fail = True
    # create / append / record must return None, never raise.
    assert await consult_persistence.create_session(pg, mode="chat", question="q") is None
    assert await consult_persistence.append_turn(
        pg, session_id="s", role="user", content="q",
    ) is None
    assert await consult_persistence.record_deep_completion(
        pg, task_id="t", answer="a",
    ) is None


@pytest.mark.asyncio
async def test_append_turn_rejects_bad_role():
    pg = _FakePg()
    sid = await consult_persistence.create_session(pg, mode="chat", question="q")
    assert await consult_persistence.append_turn(
        pg, session_id=sid, role="system", content="x",
    ) is None
    assert pg.turns == []


# ---------------------------------------------------------------------------
# Read-route tests
# ---------------------------------------------------------------------------


class _DescriptorRegistry:
    def __init__(self, pg: Any) -> None:
        self.pg = pg


def _build_app(pg: Any) -> FastAPI:
    os.environ.pop(API_TOKEN_ENV, None)
    os.environ["LEGBA_DEV_MODE"] = "1"
    deps = RegistryAPIDeps(
        descriptor_registry=_DescriptorRegistry(pg),  # type: ignore[arg-type]
        stack_registry=None,  # type: ignore[arg-type]
        vault=None,  # type: ignore[arg-type]
        dlq=None,  # type: ignore[arg-type]
        audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=None,  # type: ignore[arg-type]
        nats_store=None,
    )
    app = FastAPI()
    app.include_router(build_consult_sessions_router(deps), prefix="/api/v1")
    return app


@pytest.mark.asyncio
async def test_list_and_load_routes():
    pg = _FakePg()
    sid = await consult_persistence.create_session(
        pg, mode="chat", question="Hello world",
    )
    await consult_persistence.append_turn(pg, session_id=sid, role="user", content="Hello world")
    await consult_persistence.append_turn(pg, session_id=sid, role="assistant", content="Hi")

    client = TestClient(_build_app(pg))

    r = client.get("/api/v1/consult/sessions")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert any(row["id"] == sid for row in rows)
    assert rows[0]["turn_count"] == 2

    r = client.get(f"/api/v1/consult/sessions/{sid}")
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["id"] == sid
    assert [t["role"] for t in detail["turns"]] == ["user", "assistant"]


def test_load_route_404_for_unknown():
    pg = _FakePg()
    client = TestClient(_build_app(pg))
    r = client.get(f"/api/v1/consult/sessions/{uuid4()}")
    assert r.status_code == 404, r.text
