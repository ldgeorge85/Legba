# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""0223 — the consult turn's response bag, round-tripped on the REAL driver.

A reloaded consult session used to show the answer and nothing it was measured
against: the census, the hold, the evidence-floor stamp and the watch
candidates rode only the live response. Migration 0223 adds
``consult_turns.response``; ``consult_persistence.append_turn`` writes it and
``load_session`` lifts it. This suite proves both against Postgres through
asyncpg — the jsonb binding, the NULL for a turn with no bag, and the session
route's projection over that pool — rather than against a substring fake.
"""

from __future__ import annotations

import json
import os
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.config import PostgresConfig
from legba.data.registry import consult_persistence
from legba.data.registry.api import RegistryAPIDeps
from legba.data.registry.consult_api import ConsultResponse
from legba.data.registry.consult_sessions_api import build_consult_sessions_router

_TITLE = "test_f1_consult_turn_response"

CENSUS = {
    "version": "7g-2", "live": 0, "web_retrieval": 0, "history": 0, "seed": 0,
    "unresolved": 3, "fabricated": 1, "malformed": ["sit-001"],
    "model_knowledge": 11, "sentences_examined": 14,
}
FLOOR = {"from": 0.3, "to": 0.85, "resolved": 0}


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM consult_sessions WHERE title = $1", _TITLE)
    yield pool
    await pool.close()


class _DescriptorRegistry:
    def __init__(self, pg: Any) -> None:
        self.pg = pg


def _app(pool: Any) -> FastAPI:
    os.environ.pop("LEGBA_REGISTRY_API_TOKEN", None)
    os.environ["LEGBA_DEV_MODE"] = "1"
    deps = RegistryAPIDeps(
        descriptor_registry=_DescriptorRegistry(pool),  # type: ignore[arg-type]
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
async def test_the_response_bag_round_trips_on_the_real_driver(pg_pool):
    sid = await consult_persistence.create_session(
        pg_pool, mode="chat", question=_TITLE,
    )
    assert sid
    await consult_persistence.append_turn(
        pg_pool, session_id=sid, role="user", content="what changed?",
    )
    # A turn written the way every pre-0223 turn was: no bag.
    old_id = await consult_persistence.append_turn(
        pg_pool, session_id=sid, role="assistant", content="old answer",
    )
    # A turn written the way the live front door writes one now: the bag
    # comes off the projected ConsultResponse.
    projected = ConsultResponse(
        answer="> **Held** ...",
        uncertainty=0.85,
        model="core",
        provenance_census=CENSUS,
        hold_reason="no_live_or_held_reporting",
        uncertainty_floor_applied=FLOOR,
        watch_candidates=["a second outlet names the successor"],
    )
    new_id = await consult_persistence.append_turn(
        pg_pool, session_id=sid, role="assistant", content=projected.answer,
        response=consult_persistence.response_bag(projected),
    )
    assert old_id and new_id

    # The persisted ROW, read back raw: NULL for the old turn, the full bag
    # (every key, nulls included) for the new one.
    async with pg_pool.acquire() as conn:
        rows = {
            str(r["id"]): r["response"]
            for r in await conn.fetch(
                "SELECT id, response FROM consult_turns WHERE session_id = $1",
                sid,
            )
        }
    assert rows[old_id] is None
    stored = json.loads(rows[new_id])
    assert stored == {
        "uncertainty": 0.85,
        "provenance_census": CENSUS,
        "hold_reason": "no_live_or_held_reporting",
        "uncertainty_floor_applied": FLOOR,
        "watch_candidates": ["a second outlet names the successor"],
        "model": "core",
        "context_compaction": None,
        "watch_minted": None,
        # C-1b — the case key; None = the turn presented no case.
        "case": None,
        # C-2b-1 — the writes key; None = the turn was not armed for changes.
        "writes": None,
        # Q-1 — the turn contract; None = the run carried none.
        "contract": None,
    }

    # The session route over the same pool lifts it onto the turn.
    # (In-loop ASGI client: the pool is bound to this test's event loop, and
    # TestClient would drive the app from a second one.)
    async with AsyncClient(
        transport=ASGITransport(app=_app(pg_pool)), base_url="http://testserver",
    ) as client:
        r = await client.get(f"/api/v1/consult/sessions/{sid}")
    assert r.status_code == 200, r.text
    user, old, new = r.json()["turns"]
    assert user["response_recorded"] is False
    assert old["response_recorded"] is False
    assert old["provenance_census"] is None and old["uncertainty"] is None
    assert new["response_recorded"] is True
    assert new["provenance_census"] == CENSUS
    assert new["hold_reason"] == "no_live_or_held_reporting"
    assert new["uncertainty_floor_applied"] == FLOOR
    assert new["watch_candidates"] == ["a second outlet names the successor"]
    assert new["uncertainty"] == 0.85
    assert new["model"] == "core"


@pytest.mark.asyncio
async def test_the_context_ceiling_stamp_round_trips_on_the_real_driver(pg_pool):
    """W2 / F-6 — ``data["context_compaction"]`` is lifted by the front door's
    projection, written on the response bag, and read back onto the turn."""
    from legba.data.registry.consult_api import _project_consult_response

    stamp = {
        "input_tokens_before": 171_204, "input_tokens_after": 158_990,
        "results_dropped": 4, "ceiling": 160_000,
    }
    projected = _project_consult_response(
        {"answer": "an answer", "uncertainty": 0.4,
         "data": {"context_compaction": stamp}},
        finding_id=None, derived_from=[], model="opus",
    )
    assert projected.context_compaction == stamp
    sid = await consult_persistence.create_session(
        pg_pool, mode="chat", question=_TITLE,
    )
    turn_id = await consult_persistence.append_turn(
        pg_pool, session_id=sid, role="assistant", content=projected.answer,
        response=consult_persistence.response_bag(projected),
    )
    async with pg_pool.acquire() as conn:
        raw = await conn.fetchval(
            "SELECT response FROM consult_turns WHERE id = $1::uuid", turn_id,
        )
    assert json.loads(raw)["context_compaction"] == stamp

    async with AsyncClient(
        transport=ASGITransport(app=_app(pg_pool)), base_url="http://testserver",
    ) as client:
        r = await client.get(f"/api/v1/consult/sessions/{sid}")
    assert r.status_code == 200, r.text
    (turn,) = r.json()["turns"]
    assert turn["context_compaction"] == stamp

    # A run the ceiling did not bite projects None — absence, never a zero stamp.
    bare = _project_consult_response(
        {"answer": "a", "data": {}}, finding_id=None, derived_from=[],
    )
    assert bare.context_compaction is None


@pytest.mark.asyncio
async def test_migration_0223_is_idempotent_and_nullable(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE public.consult_turns ADD COLUMN IF NOT EXISTS response jsonb"
        )
        col = await conn.fetchrow(
            """
            SELECT data_type, is_nullable, column_default
              FROM information_schema.columns
             WHERE table_name = 'consult_turns' AND column_name = 'response'
            """
        )
    assert col is not None
    assert col["data_type"] == "jsonb"
    assert col["is_nullable"] == "YES"
    assert col["column_default"] is None
