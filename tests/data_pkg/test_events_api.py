# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the V3/P6 event read API (spec §6.2).

Covers :mod:`legba.data.registry.events_api`:

  * ``GET /api/v1/v3/events``            -> ``EventsPage``
  * ``GET /api/v1/v3/events/{id}``       -> ``EventInspectResponse``
  * ``GET /api/v1/v3/events/{id}/lifecycle`` -> ``EventLifecycleResponse``

Two layers, the ``test_v3_timeline_api`` shape:

  * PURE (no DB): route registration + no collisions with sibling routers,
    the registry-slim import guard, the cursor round-trip, the limit bound,
    and the lifecycle-vocabulary drift guard against migration 0202.
  * INTEGRATION over the ephemeral ``migrated_pg`` database + real HTTP:
    filters mirroring ``query_events`` (incl. the route-only
    ``situation_id``), the open vs ``as_of`` gate, ``include_origin``, the
    page contract (``limit`` default 50 / max 500 / ``next_cursor``), the
    dossier shape, the ledger ordering, and named refusals (400/404).

Auth: dev-mode (``LEGBA_DEV_MODE=1``, no ``LEGBA_REGISTRY_API_TOKEN``) so
``require_bearer`` returns ``"anonymous"`` — the same bearer path as the
rest of the v3 surface.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

import legba.data.registry.events_api as events_api
from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.events_api import (
    EVENT_LIFECYCLE_STATES,
    EVENTS_API_VERSION,
    MAX_LIMIT,
    _decode_cursor,
    _encode_cursor,
    build_events_router,
)
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = (
    "0011223344556677889900112233445566778899001122334455667788990011"
)
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "33" * 32)

_MIGRATIONS = (
    Path(__file__).resolve().parents[2] / "src" / "legba" / "data" / "migrations"
)


def _fixed_identity() -> SigningIdentity:
    seed = b"v3-events-api-test-signing-seed!"
    assert len(seed) == 32
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:v3-events-test",
    )


# ---------------------------------------------------------------------------
# Pure tests — registration, slimness, cursor, bounds, drift guard
# ---------------------------------------------------------------------------


def test_deploy_marker() -> None:
    assert EVENTS_API_VERSION == "2026-09/p6"


def test_events_routes_registered() -> None:
    """The three routes register and don't shadow a sibling v3 path."""
    router = build_events_router(deps=object())  # type: ignore[arg-type]
    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    assert paths == {"/events", "/events/{event_id}", "/events/{event_id}/lifecycle"}

    from legba.data.registry.belief_api import build_belief_router
    from legba.data.registry.since_api import build_since_router
    from legba.data.registry.timeline_api import build_timeline_router
    from legba.data.registry.v3_api import build_v3_router

    other = set()
    for build in (build_v3_router, build_since_router,
                  build_timeline_router, build_belief_router):
        other |= {r.path for r in build(deps=object()).routes}  # type: ignore[arg-type]
    assert not (paths & other)


def test_events_api_registry_slim_no_runtime_imports() -> None:
    """Registry-slim: no runtime / deterministic-handler imports — the
    poisoned-import guarantee the slim image's import test asserts."""
    with open(events_api.__file__, "r", encoding="utf-8") as fh:
        text = fh.read()
    import_lines = "\n".join(
        ln for ln in text.splitlines()
        if ln.strip().startswith(("import ", "from "))
    )
    assert "deterministic" not in import_lines
    assert "legba.runtime" not in import_lines and "..runtime" not in import_lines


def test_lifecycle_vocabulary_matches_migration_0202() -> None:
    """Drift guard: the API's five-state constant mirrors the events table's
    CHECK (the slim image reads no DDL — if the vocabulary grows, this test
    names the file that must follow)."""
    sql = (_MIGRATIONS / "0202_events.sql").read_text()
    m = re.search(
        r"lifecycle_state\s+text\s+NOT\s+NULL\s+DEFAULT\s+'[^']+'\s+"
        r"CHECK\s*\(\s*lifecycle_state\s+IN\s*\(([^)]+)\)",
        sql,
    )
    assert m, "0202 lifecycle CHECK not found — the drift guard's regex stale?"
    checked = tuple(v.strip().strip("'") for v in m.group(1).split(","))
    assert set(EVENT_LIFECYCLE_STATES) == set(checked)


def test_cursor_round_trip() -> None:
    when = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
    rid = uuid4()
    anchor, row_id = _decode_cursor(_encode_cursor(when, rid))
    assert anchor == when
    assert row_id == rid


def test_limit_bound_constant() -> None:
    assert MAX_LIMIT == 500


# ---------------------------------------------------------------------------
# App fixture (ephemeral migrated DB + real HTTP — the timeline-route shape)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def events_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    nats_store = NatsStore(NatsConfig.from_env())
    await nats_store.connect()

    identity = _fixed_identity()
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)

    descriptor_registry = DescriptorRegistry(
        pg_store,
        nats_store=nats_store,
        vocabulary_cache=vocab,
        signing_identity=identity,
        audit_logger=audit,
        dead_letter=dlq,
    )
    await descriptor_registry.start()

    stack_registry = StackRegistry(pg_store, vault, audit=audit, dlq=dlq)

    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry,
        stack_registry=stack_registry,
        vault=vault,
        dlq=dlq,
        audit_logger=audit,
        vocabulary_cache=vocab,
        nats_store=nats_store,
        conversion_registry=None,
    )

    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_events_router(deps), prefix="/api/v1/v3")

    yield app, deps, pg_store

    await descriptor_registry.stop()
    await nats_store.close()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(events_app):
    app, _, _ = events_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _insert_event(
    pg_store: PostgresStore,
    *,
    title: str,
    target_id: str | None = None,
    category: str = "conflict",
    lifecycle_state: str = "active",
    geo: list[str] | None = None,
    geo_lat: float | None = None,
    geo_lon: float | None = None,
    time_start: datetime | None = None,
    time_end: datetime | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    superseded_by: UUID | None = None,
    origin_class: str = "live",
    produced_at: datetime | None = None,
) -> UUID:
    eid = uuid4()
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO events (
                id, event_signature, analyst_id, title, category, severity,
                lifecycle_state, time_start, time_end, geo, geo_lat, geo_lon,
                target_id, origin_class, valid_from, valid_until,
                superseded_by, produced_at
            ) VALUES (
                $1, $2, 'p6_api_test', $3, $4, 'medium',
                $5, $6, $7, $8::text[], $9, $10,
                $11, $12, $13, $14, $15, $16
            )
            """,
            eid, f"sig-{uuid4().hex}", title, category,
            lifecycle_state, time_start, time_end, geo or [], geo_lat,
            geo_lon, target_id, origin_class,
            valid_from or datetime.now(timezone.utc) - timedelta(days=1),
            valid_until, superseded_by,
            produced_at or datetime.now(timezone.utc) - timedelta(hours=6),
        )
    return eid


async def _insert_situation(pg_store: PostgresStore, *, name: str) -> UUID:
    sid = uuid4()
    async with pg_store.acquire() as conn:
        await conn.execute(
            "INSERT INTO situations (id, data, name, status, category,"
            " intensity_score, situation_signature, valid_from, analyst_id)"
            " VALUES ($1, '{}'::jsonb, $2, 'active', 'x', 1.0, $3,"
            " now() - interval '2 days', 'situation_clustering')",
            sid, name, f"sig:{name}:{uuid4().hex[:8]}",
        )
    return sid


async def _insert_ledger(
    pg_store: PostgresStore, *, event_id: UUID, transition: str,
    state_from: str, state_to: str, occurred_at: datetime,
) -> None:
    async with pg_store.acquire() as conn:
        await conn.execute(
            "INSERT INTO event_lifecycle_events (event_id, occurred_at,"
            " transition, state_from, state_to, why, analyst_id,"
            " derived_from) VALUES ($1, $2, $3, $4, $5, 'test', 'p6', $6)",
            event_id, occurred_at, transition, state_from, state_to,
            [] if transition == "resolved" else [uuid4()],
        )


# ---------------------------------------------------------------------------
# /events — validation + page contract
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_limit_validation(client: AsyncClient):
    r = await client.get("/api/v1/v3/events", params={"limit": 0})
    assert r.status_code == 400
    r = await client.get("/api/v1/v3/events", params={"limit": 501})
    assert r.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifecycle_state_validation(client: AsyncClient):
    r = await client.get(
        "/api/v1/v3/events", params={"lifecycle_state": "bogo"})
    assert r.status_code == 400
    assert "lifecycle_state" in r.json()["detail"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_include_origin_validation(client: AsyncClient):
    r = await client.get(
        "/api/v1/v3/events",
        params={"as_of": "2026-09-01T00:00:00Z",
                "include_origin": ["live", "bogo"]},
    )
    assert r.status_code == 400
    assert "origin_class" in r.json()["detail"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_situation_id_validation(client: AsyncClient):
    r = await client.get(
        "/api/v1/v3/events", params={"situation_id": "not-a-uuid"})
    assert r.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_list_is_valid_envelope(client: AsyncClient):
    """A scope with nothing returns a 200 empty page, never a 404. Scoped to
    a unique desk — the migrated DB is session-shared."""
    desk = f"country_ev_empty_{uuid4().hex[:8]}"
    r = await client.get("/api/v1/v3/events", params={"target_id": desk})
    assert r.status_code == 200
    body = r.json()
    assert body["data"] == []
    assert body["next_cursor"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cursor_pagination(client: AsyncClient, events_app):
    _, _, pg = events_app
    desk = f"country_ev_page_{uuid4().hex[:8]}"
    ids = []
    for i in range(3):
        ids.append(await _insert_event(
            pg, title=f"e{i}", target_id=desk,
            time_start=datetime.now(timezone.utc) - timedelta(hours=i + 1),
        ))
    r1 = await client.get(
        "/api/v1/v3/events", params={"target_id": desk, "limit": 2})
    assert r1.status_code == 200
    p1 = r1.json()
    assert len(p1["data"]) == 2
    assert p1["next_cursor"] is not None
    # Newest occurrence-anchor first.
    assert p1["data"][0]["id"] == str(ids[0])

    r2 = await client.get(
        "/api/v1/v3/events",
        params={"target_id": desk, "limit": 2, "cursor": p1["next_cursor"]},
    )
    p2 = r2.json()
    assert [r["id"] for r in p2["data"]] == [str(ids[2])]
    assert p2["next_cursor"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bad_cursor_is_400(client: AsyncClient):
    r = await client.get("/api/v1/v3/events", params={"cursor": "%%%nope"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /events — filters + gates
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filters_geo_category_lifecycle(client: AsyncClient, events_app):
    _, _, pg = events_app
    desk = f"country_ev_filt_{uuid4().hex[:8]}"
    ir = await _insert_event(
        pg, title="ir strike", target_id=desk, geo=["IR"],
        category="conflict", lifecycle_state="active")
    sa = await _insert_event(
        pg, title="sa summit", target_id=desk, geo=["SA"],
        category="diplomacy", lifecycle_state="resolved")

    for params, want in (
        ({"geo": "IR"}, [ir]),
        ({"geo": ["SA", "IR"]}, [sa, ir]),
        ({"category": "diplomacy"}, [sa]),
        ({"lifecycle_state": "resolved"}, [sa]),
    ):
        r = await client.get(
            "/api/v1/v3/events", params={"target_id": desk, **params})
        assert r.status_code == 200, params
        assert [row["id"] for row in r.json()["data"]] == [
            str(x) for x in want], params


@pytest.mark.integration
@pytest.mark.asyncio
async def test_entity_and_situation_filters(client: AsyncClient, events_app):
    _, _, pg = events_app
    desk = f"country_ev_link_{uuid4().hex[:8]}"
    ev = await _insert_event(pg, title="linked", target_id=desk)
    other = await _insert_event(pg, title="unlinked", target_id=desk)

    ename = f"Entity P6 {uuid4().hex[:6]}"
    async with pg.acquire() as conn:
        ent = await conn.fetchval(
            "INSERT INTO entity_profiles (canonical_name, entity_class,"
            " entity_type, data) VALUES ($1, 'organization',"
            " 'organization', '{}'::jsonb) RETURNING id", ename)
        await conn.execute(
            "INSERT INTO event_entity_links (event_id, entity_id, role,"
            " confidence) VALUES ($1, $2, 'actor', 0.9)", ev, ent)
    sit = await _insert_situation(pg, name=f"frame {uuid4().hex[:6]}")
    async with pg.acquire() as conn:
        await conn.execute(
            "INSERT INTO situation_event_links (situation_id, event_id,"
            " relevance) VALUES ($1, $2, 1.0)", sit, ev)

    r = await client.get(
        "/api/v1/v3/events", params={"target_id": desk, "entity": "Entity P6"})
    assert [row["id"] for row in r.json()["data"]] == [str(ev)]

    r = await client.get(
        "/api/v1/v3/events", params={"situation_id": str(sit)})
    ids = [row["id"] for row in r.json()["data"]]
    assert str(ev) in ids and str(other) not in ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_open_gate_and_as_of_read(client: AsyncClient, events_app):
    _, _, pg = events_app
    desk = f"country_ev_asof_{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    head = await _insert_event(pg, title="head", target_id=desk)
    closed = await _insert_event(
        pg, title="closed", target_id=desk,
        valid_from=now - timedelta(days=3),
        valid_until=now - timedelta(hours=2),
        superseded_by=head,
    )
    web = await _insert_event(
        pg, title="web-sourced", target_id=desk, origin_class="web_retrieval",
        valid_from=now - timedelta(days=4),
    )

    r = await client.get("/api/v1/v3/events", params={"target_id": desk})
    ids = [row["id"] for row in r.json()["data"]]
    assert str(closed) not in ids  # open gate drops it
    assert str(web) in ids         # web_retrieval is a live class

    as_of = (now - timedelta(days=1)).isoformat()
    r = await client.get(
        "/api/v1/v3/events", params={"target_id": desk, "as_of": as_of})
    body = r.json()
    ids = [row["id"] for row in body["data"]]
    assert str(closed) in ids  # validity read returns it
    assert body["as_of"] is not None

    # include_origin narrows the class set (the history classes have no
    # insertable rows today — *_origin_class_history_writer_not_built is the
    # structural firewall; the clause is the reader half).
    r = await client.get(
        "/api/v1/v3/events",
        params={"target_id": desk, "as_of": as_of,
                "include_origin": ["web_retrieval"]},
    )
    ids = [row["id"] for row in r.json()["data"]]
    assert str(web) in ids and str(closed) not in ids


@pytest.mark.integration
@pytest.mark.asyncio
async def test_since_until_overlap(client: AsyncClient, events_app):
    _, _, pg = events_app
    desk = f"country_ev_win_{uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    inside = await _insert_event(
        pg, title="inside", target_id=desk,
        time_start=now - timedelta(days=2), time_end=now - timedelta(days=1),
    )
    spanning = await _insert_event(
        pg, title="spanning", target_id=desk,
        time_start=now - timedelta(days=10),
        time_end=now - timedelta(hours=2),
    )
    early = await _insert_event(
        pg, title="early", target_id=desk,
        time_start=now - timedelta(days=9), time_end=now - timedelta(days=8),
    )
    r = await client.get(
        "/api/v1/v3/events",
        params={
            "target_id": desk,
            "since": (now - timedelta(days=3)).isoformat(),
            "until": now.isoformat(),
        },
    )
    ids = [row["id"] for row in r.json()["data"]]
    assert str(inside) in ids
    assert str(spanning) in ids
    assert str(early) not in ids


# ---------------------------------------------------------------------------
# /events/{id} + /events/{id}/lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_inspect_event_sections(client: AsyncClient, events_app):
    _, _, pg = events_app
    ev = await _insert_event(
        pg, title="dossier", lifecycle_state="evolving", geo=["IR"],
        geo_lat=35.7, geo_lon=51.4,
    )
    async with pg.acquire() as conn:
        sig = await conn.fetchval(
            "INSERT INTO signals (source_id, modality, payload, content_hash,"
            " fetched_at) VALUES ('rss_main', 'text', $1::jsonb, $2, now())"
            " RETURNING id",
            json.dumps({"title": "the evidence"}),
            f"h-{uuid4().hex}",
        )
        await conn.execute(
            "INSERT INTO signal_event_links (signal_id, event_id, relevance,"
            " source_class, source_kind, source_id, linked_at) VALUES"
            " ($1, $2, 0.9, 'a', 'b', 'c', now())", sig, ev)
        ent = await conn.fetchval(
            "INSERT INTO entity_profiles (canonical_name, entity_class,"
            " entity_type, data) VALUES ($1, 'organization',"
            " 'organization', '{}'::jsonb) RETURNING id",
            f"Actor {uuid4().hex[:6]}")
        await conn.execute(
            "INSERT INTO event_entity_links (event_id, entity_id, role,"
            " confidence) VALUES ($1, $2, 'actor', 0.9)", ev, ent)
        sit = await conn.fetchval(
            "INSERT INTO situations (id, data, name, status, category,"
            " intensity_score, situation_signature, valid_from, analyst_id)"
            " VALUES ($1, '{}'::jsonb, $2, 'active', 'x', 1.0, $3, now(),"
            " 'situation_clustering') RETURNING id",
            uuid4(), f"frame {uuid4().hex[:6]}", f"s-{uuid4().hex[:6]}")
        await conn.execute(
            "INSERT INTO situation_event_links (situation_id, event_id,"
            " relevance) VALUES ($1, $2, 0.8)", sit, ev)
    await _insert_ledger(
        pg, event_id=ev, transition="opened",
        state_from="emerging", state_to="emerging",
        occurred_at=datetime.now(timezone.utc) - timedelta(days=1))
    await _insert_ledger(
        pg, event_id=ev, transition="advanced",
        state_from="emerging", state_to="evolving",
        occurred_at=datetime.now(timezone.utc) - timedelta(hours=3))

    r = await client.get(f"/api/v1/v3/events/{ev}")
    assert r.status_code == 200
    body = r.json()
    assert body["found"] is True
    assert body["event"]["id"] == str(ev)
    assert body["event"]["geo_lat"] == pytest.approx(35.7)
    assert body["signals"][0]["signal_id"] == str(sig)
    assert body["signals"][0]["title"] == "the evidence"
    assert body["actors"][0]["role"] == "actor"
    assert body["situations"][0]["id"] == str(sit)
    assert [row["transition"] for row in body["lifecycle"]] == [
        "opened", "advanced"]
    assert str(sig) in body["refs"] and str(ent) in body["refs"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_inspect_event_named_refusals(client: AsyncClient):
    r = await client.get("/api/v1/v3/events/not-a-uuid")
    assert r.status_code == 400
    r = await client.get(f"/api/v1/v3/events/{uuid4()}")
    assert r.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifecycle_route_oldest_first(client: AsyncClient, events_app):
    _, _, pg = events_app
    ev = await _insert_event(pg, title="ledger", lifecycle_state="resolved")
    await _insert_ledger(
        pg, event_id=ev, transition="advanced",
        state_from="emerging", state_to="active",
        occurred_at=datetime.now(timezone.utc) - timedelta(hours=5))
    await _insert_ledger(
        pg, event_id=ev, transition="opened",
        state_from="emerging", state_to="emerging",
        occurred_at=datetime.now(timezone.utc) - timedelta(days=1))

    r = await client.get(f"/api/v1/v3/events/{ev}/lifecycle")
    assert r.status_code == 200
    body = r.json()
    assert body["event_id"] == str(ev)
    # OLDEST first — `opened` leads even though it was inserted second.
    assert [row["transition"] for row in body["rows"]] == [
        "opened", "advanced"]
    assert body["rows"][0]["why"] == "test"

    r = await client.get(f"/api/v1/v3/events/{uuid4()}/lifecycle")
    assert r.status_code == 404
