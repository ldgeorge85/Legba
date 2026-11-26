# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A record row that entered the record after K is invisible to a cut read.

Database-backed (isolated test stack). For every record reader the record-only
arm can call, and for ``live_series``, one row recorded before K and one after,
read through the PRODUCTION port and the PRODUCTION dispatcher
(``consult_tool_routing._dispatch_tool``) with ``known_at=K``: only the early
row comes back; without the cut both do (or today's state does). Where the
reader has a supersession clock, the row that was the live head at K (and was
superseded after K) is what the cut read returns.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.consult_tool_routing import _dispatch_tool
from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

KAT = datetime(2026, 11, 5, 7, tzinfo=timezone.utc)
EARLY, LATE = KAT - timedelta(days=2), KAT + timedelta(hours=1)
_TAG = f"e2cut{uuid4().hex[:8]}"
_COLLECTION = f"collection.{_TAG}"


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database, min_size=1, max_size=2,
    )
    yield p
    async with p.acquire() as conn:  # history rows only: live rows are append-only by trigger
        await conn.execute("DELETE FROM observations WHERE collection_id = $1", _COLLECTION)
        await conn.execute("DELETE FROM collection_descriptors WHERE descriptor_id = $1", _COLLECTION)
    await p.close()


def _call(port, known_at=KAT):
    async def call(name, args):
        return await _dispatch_tool(port, name=name, args=args, scope_predicate=None, known_at=known_at)
    return call


async def _finding(conn, *, title, target, created_at, superseded_at=None):
    oid = uuid4()
    await conn.execute(
        """
        INSERT INTO analyst_outputs (id, kind, title, body, confidence, severity, data,
            target_id, produced_at, created_at, superseded_at, derived_from, schema_uri)
        VALUES ($1, 'finding', $2, 'body', 0.8, 'medium', '{}'::jsonb, $3, $4, $4, $5,
                '{}'::uuid[], 'iglu:legba/finding/jsonschema/1-0-0')
        """, oid, title, target, created_at, superseded_at)
    return str(oid)


async def _fact(conn, *, subject, value, created_at, valid_until=None):
    fid = uuid4()
    await conn.execute(
        """
        INSERT INTO facts (id, subject, predicate, value, confidence, source_type,
            valid_from, valid_until, produced_at, created_at, derived_from, schema_uri)
        VALUES ($1, $2, 'leader', $3, 0.9, 'agent', $4, $5, $4, $4, '{}'::uuid[],
                'iglu:legba/fact/jsonschema/2-0-0')
        """, fid, subject, value, created_at, valid_until)
    return str(fid)


async def _signal(conn, *, token, created_at):
    sid = uuid4()
    await conn.execute(
        """
        INSERT INTO signals (id, source_id, source_version, produced_by_kind, fetched_at, created_at,
            updated_at, modality, payload, canonical_url, language, tags, derived_from, schema_uri)
        VALUES ($1, 'rss_main', '', 'source', $2, $2, $2, 'text', $3::jsonb, $4, 'en',
                '{}'::text[], '{}'::uuid[], 'iglu:legba/signal/jsonschema/3-0-0')
        """, sid, created_at, json.dumps({"title": f"{token} report"}),
        f"https://example.invalid/e2/{sid}")
    return str(sid)


async def _event(conn, *, title, target, created_at):
    eid = uuid4()
    await conn.execute(
        """
        INSERT INTO events (id, event_signature, analyst_id, title, summary, category, event_type,
            severity, lifecycle_state, lifecycle_changed_at, time_start, geo, locations, confidence,
            signal_count, distinct_source_count, oversized, source_method, source_type, target_id,
            target_version, analyst_version, origin_class, valid_from, produced_at, created_at, updated_at)
        VALUES ($1, $2, 'e2_test', $3, 'sum', 'conflict', 'incident', 'medium', 'resolved', $5,
                $4, '{}'::text[], '{}', 0.8, 7, 3, false, 'clustering', 'agent', $6, 'v1', 0, 'live',
                $4, $4, $4, $5)
        """, eid, f"sig-{uuid4().hex}", title, created_at, LATE, target)
    return str(eid)


# ---------------------------------------------------------------------------
# findings: list_findings, belief_as_of
# ---------------------------------------------------------------------------


async def test_findings_recorded_after_known_at_are_invisible(pool):
    target = f"country_{_TAG}_f"
    async with pool.acquire() as conn:
        # the live head at K, superseded after K by a finding recorded after K
        early = await _finding(conn, title="early", target=target, created_at=EARLY, superseded_at=LATE)
        late = await _finding(conn, title="late", target=target, created_at=LATE)
        await conn.execute("UPDATE analyst_outputs SET superseded_by = $1::uuid WHERE id = $2::uuid",
                           late, early)
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pool, qdrant_client=None)
    cut, uncut = _call(port), _call(port, None)

    assert (await uncut("list_findings", {"target_id": target}))["refs"] == [late]
    out = await cut("list_findings", {"target_id": target})
    assert out["refs"] == [early] and out["known_at_cut"]["known_at"] == KAT.isoformat()
    out = await cut("list_findings", {"target_id": target, "include_superseded": True})
    assert out["refs"] == [early]
    assert set((await uncut("list_findings", {"target_id": target, "include_superseded": True}))["refs"]) == {early, late}
    # belief_as_of: a date after K is read at K
    out = await cut("belief_as_of", {"as_of": (KAT + timedelta(days=10)).isoformat(), "target_id": target})
    assert out["refs"] == [early]
    assert (await uncut("belief_as_of", {"as_of": (KAT + timedelta(days=10)).isoformat(),
                                        "target_id": target}))["refs"] == [late]


# ---------------------------------------------------------------------------
# facts and entities: query_facts, inspect_entity
# ---------------------------------------------------------------------------


async def test_facts_and_entity_rows_recorded_after_known_at_are_invisible(pool):
    name = f"E2 Entity {_TAG}"
    async with pool.acquire() as conn:
        # held at K, closed after K
        early_fact = await _fact(conn, subject=name, value="early", created_at=EARLY, valid_until=LATE)
        late_fact = await _fact(conn, subject=name, value="late", created_at=LATE)
        ent = await conn.fetchval(
            "INSERT INTO entity_profiles (canonical_name, entity_class, entity_type, data, version, "
            "created_at) VALUES ($1, 'organization', 'organization', '{}'::jsonb, 2, $2) RETURNING id",
            name, KAT - timedelta(days=5))
        v_early = await conn.fetchval(
            "INSERT INTO entity_profile_versions (entity_id, version, data, created_at) "
            "VALUES ($1, 1, '{}'::jsonb, $2) RETURNING id", ent, EARLY)
        v_late = await conn.fetchval(
            "INSERT INTO entity_profile_versions (entity_id, version, data, created_at) "
            "VALUES ($1, 2, '{}'::jsonb, $2) RETURNING id", ent, LATE)
        s_early = await _signal(conn, token=_TAG, created_at=EARLY)
        s_late = await _signal(conn, token=_TAG, created_at=LATE)
        for sid, at in ((s_early, EARLY), (s_late, LATE)):
            await conn.execute(
                "INSERT INTO signal_entity_links (signal_id, entity_id, role, confidence, created_at) "
                "VALUES ($1::uuid, $2, 'subject', 0.9, $3)", sid, ent, at)
        late_name = f"E2 Late {_TAG}"
        await conn.execute(
            "INSERT INTO entity_profiles (canonical_name, entity_class, entity_type, data, created_at) "
            "VALUES ($1, 'organization', 'organization', '{}'::jsonb, $2)", late_name, LATE)
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pool, qdrant_client=None)
    cut, uncut = _call(port), _call(port, None)

    assert (await uncut("query_facts", {"subject": name}))["refs"] == [late_fact]
    out = await cut("query_facts", {"subject": name})
    assert out["refs"] == [early_fact]

    out = await cut("inspect_entity", {"name": name})
    assert out["found"] is True
    assert [v["id"] for v in out["versions"]] == [str(v_early)]
    assert [m["signal_id"] for m in out["recent_signal_mentions"]] == [s_early]
    assert [f["id"] for f in out["facts"]] == [early_fact]
    assert out["profile"]["version"] == 1
    uncut_out = await uncut("inspect_entity", {"name": name})
    assert str(v_late) in uncut_out["refs"] and s_late in uncut_out["refs"]
    assert (await cut("inspect_entity", {"name": late_name}))["found"] is False
    assert (await uncut("inspect_entity", {"name": late_name}))["found"] is True


# ---------------------------------------------------------------------------
# events: query_events, inspect_event
# ---------------------------------------------------------------------------


async def test_events_and_their_sections_recorded_after_known_at_are_invisible(pool):
    target = f"country_{_TAG}_e"
    async with pool.acquire() as conn:
        early = await _event(conn, title="early event", target=target, created_at=EARLY)
        late = await _event(conn, title="late event", target=target, created_at=LATE)
        for eid, at in ((early, EARLY), (late, LATE)):
            await conn.execute(
                "INSERT INTO event_lifecycle_events (event_id, occurred_at, transition, state_from, "
                "state_to, why, analyst_id, derived_from, created_at) "
                "VALUES ($1::uuid, $2, 'opened', 'emerging', 'emerging', 'test', 'e2', $3, $2)",
                eid, at, [uuid4()])
        await conn.execute(
            "INSERT INTO event_lifecycle_events (event_id, occurred_at, transition, state_from, "
            "state_to, why, analyst_id, derived_from, created_at) "
            "VALUES ($1::uuid, $2, 'resolved', 'emerging', 'resolved', 'test', 'e2', '{}'::uuid[], $2)",
            early, LATE)
        s_early = await _signal(conn, token=_TAG, created_at=EARLY)
        s_late = await _signal(conn, token=_TAG, created_at=LATE)
        for sid, at in ((s_early, EARLY), (s_late, LATE)):
            await conn.execute(
                "INSERT INTO signal_event_links (signal_id, event_id, relevance, source_class, "
                "source_kind, source_id, linked_at, created_at) "
                "VALUES ($1::uuid, $2::uuid, 0.9, 'a', 'b', $3, $4, $4)", sid, early, f"src_{sid[:6]}", at)
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pool, qdrant_client=None)
    cut, uncut = _call(port), _call(port, None)

    assert set((await uncut("query_events", {"target_id": target}))["refs"]) == {early, late}
    out = await cut("query_events", {"target_id": target})
    assert out["refs"] == [early]
    row = out["rows"][0]
    # rebuilt at K: the ledger said emerging, one link, the post-K close masked
    assert row["lifecycle_state"] == "emerging" and row["signal_count"] == 1
    assert row["distinct_source_count"] == 1
    assert (await cut("query_events", {"target_id": target, "lifecycle_state": "resolved"}))["refs"] == []
    assert (await uncut("query_events", {"target_id": target, "lifecycle_state": "resolved"}))["refs"]

    dossier = await cut("inspect_event", {"event_id": early})
    assert dossier["found"] is True
    assert [s["signal_id"] for s in dossier["signals"]] == [s_early]
    assert [r["state_to"] for r in dossier["lifecycle"]] == ["emerging"]
    assert dossier["event"]["lifecycle_state"] == "emerging" and dossier["event"]["updated_at"] is None
    assert s_late not in dossier["refs"]
    assert s_late in (await uncut("inspect_event", {"event_id": early}))["refs"]
    gone = await cut("inspect_event", {"event_id": late})
    assert gone["found"] is False and gone["refs"] == []


# ---------------------------------------------------------------------------
# series: series_history / series_compare (history), live_series (live)
# ---------------------------------------------------------------------------


async def test_series_rows_loaded_after_known_at_are_invisible(pool):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO collection_descriptors (descriptor_id, version, schema_uri, abstraction_level,
                state, owner, name, body, is_head, origin_shape, origin_class, licence_class,
                collection_version)
            VALUES ($1, $2, 'legba/collection/1.0.0', 'L0', 'loaded', 'test_e2', 'e2', '{}'::jsonb,
                    TRUE, 'archive_only', 'archive', 'public', 'v1')
            """, _COLLECTION, "0" * 64)
        ins = ("INSERT INTO observations (collection_id, series_id, subject_kind, subject, valid_from, "
               "valid_to, record_time, loaded_at, value, unit, source_url, sha256, origin_class) "
               "VALUES ($1, 'e2.s', 'country', 'IR', $2, $2, $3, $4, $5, 'pct', 'https://x.invalid', "
               "'deadbeef', 'archive') RETURNING id")
        d = date(2026, 1, 1)
        early = await conn.fetchval(ins, _COLLECTION, d, EARLY - timedelta(days=5), EARLY, Decimal("1.0"))
        # a restatement of the same period, published and loaded after K
        late = await conn.fetchval(ins, _COLLECTION, d, LATE, LATE, Decimal("2.0"))
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pool, qdrant_client=None)
    cut, uncut = _call(port), _call(port, None)
    args = {"series_id": "e2.s", "subject": "IR", "from": "2025-01-01", "to": "2026-12-31",
            "collection_id": _COLLECTION}
    assert (await uncut("series_history", args))["refs"] == [str(late)]
    out = await cut("series_history", {**args, "as_of": "2030-01-01"})
    assert out["refs"] == [str(early)] and out["as_of"] == KAT.isoformat()
    out = await cut("series_compare", {**{k: v for k, v in args.items() if k != "subject"}, "subjects": ["IR"]})
    assert out["refs"] == [str(early)]


async def test_live_series_rows_recorded_after_known_at_are_invisible(pool):
    source = f"source.{_TAG}.live"
    ins = ("INSERT INTO observations (collection_id, series_id, subject_kind, subject, valid_from, "
           "valid_to, record_time, loaded_at, value, unit, source_url, sha256, origin_class, source_id, "
           "methodology_version, fetch_sha256, period, record_time_basis) "
           "VALUES ($1, 'e2.rate', 'country', 'US', $2, $2, $3, $3, $4, 'percent', 'https://x.invalid', "
           "$5, 'live', $6, 'm1', $5, 'day', 'provider') RETURNING id")
    async with pool.acquire() as conn:
        early = await conn.fetchval(ins, f"live:{source}", date(2026, 11, 1), EARLY, Decimal("4.5"),
                                    "a" * 64, source)
        # a revision of the same period, recorded after K
        late = await conn.fetchval(ins, f"live:{source}", date(2026, 11, 1), LATE, Decimal("4.25"),
                                   "b" * 64, source)
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pool, qdrant_client=None)
    cut, uncut = _call(port), _call(port, None)
    win = {"series_id": "e2.rate", "subject": "US", "source_id": source, "from": "2026-10-01", "to": "2026-11-30"}

    out = await uncut("live_series", win)
    assert out["refs"] == [str(early), str(late)] and out["revised_periods"] == 1
    assert all(r["origin_class"] == "live" and r["source_id"] == source for r in out["rows"])
    out = await cut("live_series", win)
    assert out["refs"] == [str(early)] and out["rows"][0]["is_latest_revision"] is True
    cat = await cut("live_series", {"source_id": source})
    assert [s["series_id"] for s in cat["series"]] == ["e2.rate"]
    assert cat["series"][0]["rows_held"] == 1
    assert datetime.fromisoformat(cat["series"][0]["newest_loaded_at"]) == EARLY
    # the history tools never see a live row, cut or not
    hist = await uncut("series_history", {"series_id": "e2.rate", "subject": "US", "from": "2026-01-01",
                                         "to": "2026-12-31"})
    assert str(early) not in hist.get("refs", []) and str(late) not in hist.get("refs", [])
