# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A row that entered the record after K is invisible to a cut read.

Database-backed (isolated test stack). Two signals carry the same unique token;
one was created before K, one after. Through the production port and the
production dispatcher with ``known_at=K``:

* ``search_signals`` returns only the earlier row (without the cut, both);
* ``read_document`` returns no body for the later row (Postgres namespace);
* ``list_sources`` reports the source's newest signal as the earlier one;
* the confirmation query keeps the earlier id and refuses the later one.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.consult_tool_routing import _dispatch_tool
from legba.runtime import substrate_known_at as cut
from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort

KAT = datetime(2026, 11, 5, 7, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database, min_size=1, max_size=2,
    )
    yield p
    await p.close()


async def _signal(pool, *, token: str, source_id: str, created_at: datetime) -> str:
    sid = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO signals (
                id, source_id, source_version, produced_by_kind,
                fetched_at, created_at, updated_at, modality, payload, canonical_url,
                language, tags, derived_from, schema_uri
            ) VALUES (
                $1, $2, '', 'source', $3, $3, $3, 'text', $4::jsonb, $5, 'en',
                '{}'::text[], '{}'::uuid[], 'iglu:legba/signal/jsonschema/3-0-0'
            )
            """,
            sid, source_id, created_at,
            json.dumps({"title": f"{token} report", "summary": f"{token} body text"}),
            f"https://example.invalid/e0/{sid}",
        )
    return str(sid)


async def _source(pool, source_id: str) -> None:
    body = {"identity": {"id": source_id, "kind": "rss", "state": "active"}}
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO source_descriptors
                (descriptor_id, version, schema_uri, is_head, kind, state, owner, name, body, created_at)
            VALUES ($1, 'v1', 'legba/source/1.0.0', TRUE, 'rss', 'active', 'test', $1, $2::jsonb, $3)
            """,
            source_id, json.dumps(body), KAT - timedelta(days=60),
        )


@pytest.mark.asyncio
async def test_a_row_created_after_known_at_is_invisible(pool):
    token = "e0cut" + uuid4().hex[:10]
    source_id = f"source.e0cut.{uuid4().hex[:8]}"
    await _source(pool, source_id)
    early_at, late_at = KAT - timedelta(days=2), KAT + timedelta(hours=1)
    early = await _signal(pool, token=token, source_id=source_id, created_at=early_at)
    late = await _signal(pool, token=token, source_id=source_id, created_at=late_at)
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pool, qdrant_client=None)

    async def call(name, args, known_at=KAT):
        return await _dispatch_tool(port, name=name, args=args, scope_predicate=None, known_at=known_at)

    # search_signals: both without the cut, only the earlier one with it
    uncut = await call("search_signals", {"query": token, "limit": 50}, known_at=None)
    assert {early, late} <= set(uncut["refs"])
    out = await call("search_signals", {"query": token, "limit": 50})
    assert early in out["refs"] and late not in out["refs"]
    assert out["known_at_cut"]["known_at"] == KAT.isoformat()

    # read_document (no corpus wired: the Postgres namespace answers)
    doc = await call("read_document", {"doc_id": late})
    assert doc["status"] == "not_in_record_at_known_at" and doc["refs"] == []
    assert token not in json.dumps(doc)
    doc = await call("read_document", {"doc_id": early})
    assert doc["refs"] == [early]

    # list_sources: the newest signal at K is the earlier one
    srcs = await call("list_sources", {"active_only": True})
    row = next(r for r in srcs["rows"] if r["source_id"] == source_id)
    assert datetime.fromisoformat(row["last_signal_at"]) == early_at
    uncut_srcs = await call("list_sources", {"active_only": True}, known_at=None)
    row = next(r for r in uncut_srcs["rows"] if r["source_id"] == source_id)
    assert datetime.fromisoformat(row["last_signal_at"]) == late_at

    # the confirmation query
    kept, refused = await cut.confirm_recorded_before(pool, [early, late], KAT)
    assert kept == {early} and refused == {late}
