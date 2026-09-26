# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed tests for the V3/P6 event readers (spec §6.1).

A real ``PostgresQdrantSubstrateQueryPort`` over the ephemeral migrated
database — the no-mocks posture of ``test_as_of_readers.py`` /
``test_substrate_query_port.py``. The load-bearing claims:

* The default read is the OPEN gate — ``superseded`` / ``valid_until``
  -closed / unclassified-origin events are absent; a backfilled event can
  never read as live.
* ``as_of`` swaps to the validity predicate (a row closed today IS the
  answer on date D) — and only there does ``include_origin`` widen the
  class set; an unknown class or lifecycle state refuses loud.
* ``since``/``until`` bound the OCCURRENCE span by overlap (an event that
  began before ``until`` and had not ended before ``since`` is in-window;
  NULL bounds fall back to ``produced_at``).
* ``geo`` / ``category`` / ``entity`` / ``situation_id`` filter on the real
  join tables.
* ``inspect_event`` returns the five sections — event, ranked signals,
  actors with roles, edges (both directions), tracking situations, the
  lifecycle ledger OLDEST→NEWEST — and ``found=False`` + a named error for
  a missing or malformed id, never an empty-looking success.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort

pytestmark = [pytest.mark.asyncio]


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    pool = await asyncpg.create_pool(
        host=migrated_pg.host,
        port=migrated_pg.port,
        user=migrated_pg.user,
        password=migrated_pg.password,
        database=migrated_pg.database,
        min_size=1,
        max_size=4,
    )
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def port(pg_pool):
    return PostgresQdrantSubstrateQueryPort(
        pg_pool=pg_pool, qdrant_client=None,
        signals_collection="legba_test_p6ev__signals")


_NOW = datetime.now(timezone.utc)


async def _event(
    pool, *,
    title: str,
    analyst_id: str | None = None,
    category: str = "conflict",
    lifecycle_state: str = "active",
    time_start: datetime | None = None,
    time_end: datetime | None = None,
    geo: list[str] | None = None,
    geo_lat: float | None = None,
    geo_lon: float | None = None,
    target_id: str | None = None,
    severity: str = "medium",
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    superseded_by=None,
    origin_class: str = "live",
    produced_at: datetime | None = None,
):
    """One ``events`` row with controlled knobs. ``event_signature`` is
    unique per call so the (event_signature, analyst_id) uniqueness never
    collides across tests."""
    eid = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO events (
                id, event_signature, analyst_id, title, summary, category,
                event_type, severity, lifecycle_state, lifecycle_changed_at,
                time_start, time_end, geo, geo_lat, geo_lon, locations,
                confidence, signal_count, distinct_source_count, oversized,
                source_method, source_type, target_id, target_version,
                analyst_version, origin_class, collection_id,
                valid_from, valid_until, superseded_by, produced_at
            ) VALUES (
                $1, $2, $3, $4, 'sum', $5,
                'incident', $6, $7, now(),
                $8, $9, $10::text[], $11, $12, '{}',
                0.8, 0, 0, false,
                'clustering', 'agent', $13, 'v1',
                0, $14, NULL,
                $15, $16, $17, $18
            )
            """,
            eid, f"sig-{uuid4().hex}", analyst_id or f"an_{uuid4().hex[:8]}",
            title, category, severity, lifecycle_state,
            time_start, time_end, geo or [], geo_lat, geo_lon,
            target_id, origin_class,
            valid_from or _NOW - timedelta(days=1), valid_until,
            superseded_by, produced_at or _NOW - timedelta(hours=12),
        )
    return eid


async def _signal(pool):
    """One minimal ``signals`` row (source 'rss_main' is seeded by the
    migrated schema)."""
    async with pool.acquire() as conn:
        return await conn.fetchval(
            "INSERT INTO signals (source_id, modality, payload, content_hash,"
            " fetched_at) VALUES ('rss_main', 'text', $1::jsonb, $2, now())"
            " RETURNING id",
            json.dumps({"title": "seed signal"}),
            f"h-{uuid4().hex}",
        )


async def _link_signal(pool, *, signal_id, event_id, relevance: float):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO signal_event_links (signal_id, event_id, relevance,"
            " source_class, source_kind, source_id, linked_at)"
            " VALUES ($1, $2, $3, 'a', 'b', 'c', now())",
            signal_id, event_id, relevance,
        )


async def _entity(pool, *, name: str):
    async with pool.acquire() as conn:
        eid = await conn.fetchval(
            "SELECT id FROM entity_profiles "
            " WHERE lower(canonical_name)=lower($1) LIMIT 1", name)
        if eid is None:
            eid = await conn.fetchval(
                "INSERT INTO entity_profiles (canonical_name, entity_class,"
                " entity_type, data) VALUES ($1, 'organization',"
                " 'organization', '{}'::jsonb) RETURNING id", name)
        return eid


async def _link_actor(pool, *, entity_id, event_id, role: str = "actor",
                      confidence: float = 0.9):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO event_entity_links (event_id, entity_id, role,"
            " confidence) VALUES ($1, $2, $3, $4)",
            event_id, entity_id, role, confidence,
        )


async def _situation(pool, *, name: str):
    sid = uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO situations (id, data, name, status, category,"
            " intensity_score, situation_signature, valid_from, analyst_id)"
            " VALUES ($1, '{}'::jsonb, $2, 'active', 'x', 1.7, $3,"
            " $4, 'situation_clustering')",
            sid, name, f"sig:{name}:{uuid4().hex[:8]}",
            _NOW - timedelta(days=2),
        )
    return sid


async def _link_situation(pool, *, situation_id, event_id, relevance=1.0):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO situation_event_links (situation_id, event_id,"
            " relevance) VALUES ($1, $2, $3)",
            situation_id, event_id, relevance,
        )


async def _ledger(pool, *, event_id, transition: str, state_from: str,
                  state_to: str, occurred_at: datetime, why: str = "test"):
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO event_lifecycle_events (event_id, occurred_at,"
            " transition, state_from, state_to, why, analyst_id,"
            " derived_from) VALUES ($1, $2, $3, $4, $5, $6, 'p6', $7)",
            event_id, occurred_at, transition, state_from, state_to, why,
            [] if transition == "resolved" else [uuid4()],
        )


def _titles(out: dict) -> list[str]:
    return [r["title"] for r in out["rows"]]


def _ids(out: dict) -> list[str]:
    return [r["id"] for r in out["rows"]]


# ---------------------------------------------------------------------------
# query_events — gates + filters
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_open_gate_excludes_superseded_and_closed(pg_pool, port):
    desk = f"country_p6_gate_{uuid4().hex[:8]}"
    live = await _event(pg_pool, title="live", target_id=desk)
    head = await _event(pg_pool, title="head", target_id=desk)
    dead = await _event(
        pg_pool, title="superseded", target_id=desk,
        superseded_by=head, valid_until=_NOW - timedelta(hours=1),
    )
    out = await port.query_events(target_id=desk)
    assert str(live) in _ids(out)
    assert str(dead) not in _ids(out)


@pytest.mark.integration
async def test_as_of_swaps_to_validity_predicate(pg_pool, port):
    """An event superseded TODAY is the answer on date D — the as_of read
    returns it, the open read does not."""
    desk = f"country_p6_asof_{uuid4().hex[:8]}"
    head = await _event(pg_pool, title="head", target_id=desk)
    closed = await _event(
        pg_pool, title="closed-today", target_id=desk,
        valid_from=_NOW - timedelta(days=3),
        valid_until=_NOW - timedelta(hours=2),
        superseded_by=head,
    )
    open_out = await port.query_events(target_id=desk)
    assert str(closed) not in _ids(open_out)

    as_of_out = await port.query_events(
        target_id=desk, as_of=(_NOW - timedelta(days=1)).isoformat())
    assert str(closed) in _ids(as_of_out)
    assert as_of_out["as_of"] is not None


@pytest.mark.integration
async def test_origin_class_gate_on_the_event_surface(pg_pool, port):
    """The P7 firewall reaches the event surface. ``web_retrieval`` /
    ``seed`` are live classes — both read on the open gate; an
    ``include_origin`` narrows the class set on the as_of read, and an
    unknown class refuses loud. (The three history classes —
    backfill_native/backfill_reconstructed/archive — cannot be INSERTED at
    all today: ``events_origin_class_history_writer_not_built`` (SEAMS #57) is the
    structural half of the same firewall, so there is no row for them to
    hide — the clause is the reader half and is what this test pins.)"""
    desk = f"country_p6_oc_{uuid4().hex[:8]}"
    web = await _event(
        pg_pool, title="web-sourced", target_id=desk,
        origin_class="web_retrieval")
    seed = await _event(
        pg_pool, title="seeded", target_id=desk, origin_class="seed")
    ids = _ids(await port.query_events(target_id=desk))
    assert str(web) in ids and str(seed) in ids

    # include_origin narrows to ONLY the named classes on the as_of read.
    as_of = _NOW.isoformat()
    out = await port.query_events(
        target_id=desk, as_of=as_of, include_origin=["seed"])
    assert _ids(out) == [str(seed)]
    # …and asking for a history class (no rows possible) is a valid narrow,
    # not an error.
    out = await port.query_events(
        target_id=desk, as_of=as_of, include_origin=["archive"])
    assert _ids(out) == []

    # An unknown class refuses — the same firewall as every other reader.
    bad = await port.query_events(
        target_id=desk, as_of=as_of, include_origin=["nonsense"])
    assert "error" in bad
    assert "origin_class" in bad["error"]


@pytest.mark.integration
async def test_lifecycle_state_filter_and_refusal(pg_pool, port):
    desk = f"country_p6_lc_{uuid4().hex[:8]}"
    resolved = await _event(
        pg_pool, title="done", target_id=desk, lifecycle_state="resolved")
    await _event(pg_pool, title="going", target_id=desk,
                 lifecycle_state="active")
    out = await port.query_events(target_id=desk, lifecycle_state="resolved")
    assert _ids(out) == [str(resolved)]

    bad = await port.query_events(target_id=desk, lifecycle_state="bogo")
    assert "error" in bad
    assert "lifecycle_state" in bad["error"]


@pytest.mark.integration
async def test_geo_category_entity_filters(pg_pool, port):
    desk = f"country_p6_filt_{uuid4().hex[:8]}"
    ir = await _event(
        pg_pool, title="iran strike", target_id=desk, geo=["IR"],
        category="conflict")
    sa = await _event(
        pg_pool, title="saudi summit", target_id=desk, geo=["SA"],
        category="diplomacy")
    actor = await _entity(pg_pool, name=f"Acme P6 {uuid4().hex[:6]}")
    await _link_actor(pg_pool, entity_id=actor, event_id=ir, role="actor")

    assert _ids(await port.query_events(target_id=desk, geo="IR")) == [str(ir)]
    assert _ids(await port.query_events(target_id=desk, geo=["SA"])) == [str(sa)]
    assert _ids(
        await port.query_events(target_id=desk, category="diplomacy")
    ) == [str(sa)]
    # The entity filter resolves the actor's canonical name through the
    # links table — the saudi event has no actor link.
    ent_out = await port.query_events(target_id=desk, entity="Acme P6")
    assert _ids(ent_out) == [str(ir)]


@pytest.mark.integration
async def test_since_until_occurrence_overlap(pg_pool, port):
    """Window = span OVERLAP: an event that began before `until` and had not
    ended before `since` is in the window — the honest "what was happening"
    read, not an anchored-in-window cut."""
    desk = f"country_p6_win_{uuid4().hex[:8]}"
    inside = await _event(
        pg_pool, title="inside", target_id=desk,
        time_start=_NOW - timedelta(days=2),
        time_end=_NOW - timedelta(days=1),
    )
    spanning = await _event(
        pg_pool, title="spanning", target_id=desk,
        time_start=_NOW - timedelta(days=10),  # started BEFORE `since`
        time_end=_NOW - timedelta(hours=2),     # ended inside the window
    )
    ended_before = await _event(
        pg_pool, title="too early", target_id=desk,
        time_start=_NOW - timedelta(days=9),
        time_end=_NOW - timedelta(days=8),
    )
    since = (_NOW - timedelta(days=3)).isoformat()
    until = _NOW.isoformat()
    out = await port.query_events(target_id=desk, since=since, until=until)
    ids = _ids(out)
    assert str(inside) in ids
    assert str(spanning) in ids        # overlap, not anchored-in
    assert str(ended_before) not in ids

    # A malformed bound refuses rather than widening to all-time.
    bad = await port.query_events(target_id=desk, since="not-a-date")
    assert "error" in bad


@pytest.mark.integration
async def test_situation_id_returns_tracked_events(pg_pool, port):
    desk = f"country_p6_sit_{uuid4().hex[:8]}"
    tracked = await _event(pg_pool, title="tracked", target_id=desk)
    other = await _event(pg_pool, title="unrelated", target_id=desk)
    sit = await _situation(pg_pool, name=f"frame {uuid4().hex[:6]}")
    await _link_situation(pg_pool, situation_id=sit, event_id=tracked)

    out = await port.query_events(situation_id=str(sit))
    assert str(tracked) in _ids(out)
    assert str(other) not in _ids(out)

    bad = await port.query_events(situation_id="not-a-uuid")
    assert "error" in bad


@pytest.mark.integration
async def test_unbounded_start_counts_null_valid_from(pg_pool, port):
    desk = f"country_p6_ubs_{uuid4().hex[:8]}"
    async with pg_pool.acquire() as conn:
        eid = await conn.fetchval(
            "INSERT INTO events (event_signature, analyst_id, title,"
            " valid_from, target_id)"
            " VALUES ($1, 'p6', 'null-vf', NULL, $2) RETURNING id",
            f"sig-{uuid4().hex}", desk)
    as_of = (_NOW + timedelta(hours=1)).isoformat()
    out = await port.query_events(target_id=desk, as_of=as_of)
    assert str(eid) in _ids(out)
    assert out["unbounded_start"] >= 1


# ---------------------------------------------------------------------------
# inspect_event — the five-section dossier
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_inspect_event_full_dossier(pg_pool, port):
    ev = await _event(pg_pool, title="the strike", lifecycle_state="evolving")
    other = await _event(pg_pool, title="the prelude",
                         lifecycle_state="resolved")

    s_hi = await _signal(pg_pool)
    s_lo = await _signal(pg_pool)
    await _link_signal(pg_pool, signal_id=s_hi, event_id=ev, relevance=0.9)
    await _link_signal(pg_pool, signal_id=s_lo, event_id=ev, relevance=0.4)

    actor = await _entity(pg_pool, name=f"Actor P6 {uuid4().hex[:6]}")
    await _link_actor(pg_pool, entity_id=actor, event_id=ev, role="actor")

    async with pg_pool.acquire() as conn:
        edge = await conn.fetchval(
            "INSERT INTO event_edges (src_event_id, dst_event_id, edge_type,"
            " confidence, why, derived_from) VALUES ($1, $2, 'evolves_from',"
            " 0.8, 'test edge', $3) RETURNING id",
            other, ev, [uuid4()],
        )
    sit = await _situation(pg_pool, name=f"watch {uuid4().hex[:6]}")
    await _link_situation(pg_pool, situation_id=sit, event_id=ev,
                          relevance=0.7)

    await _ledger(
        pg_pool, event_id=ev, transition="opened",
        state_from="emerging", state_to="emerging",
        occurred_at=_NOW - timedelta(days=2))
    await _ledger(
        pg_pool, event_id=ev, transition="advanced",
        state_from="emerging", state_to="evolving",
        occurred_at=_NOW - timedelta(hours=6))

    out = await port.inspect_event(event_id=str(ev))

    assert out["found"] is True
    assert out["event"]["id"] == str(ev)
    assert out["event"]["lifecycle_state"] == "evolving"

    # Signals ranked by relevance — the evidence, not the summary.
    assert [s["signal_id"] for s in out["signals"]] == [str(s_hi), str(s_lo)]
    assert out["signals"][0]["relevance"] == pytest.approx(0.9)
    assert out["signals"][0]["title"] == "seed signal"

    # Actors carry role + confidence through the entity join.
    assert out["actors"][0]["entity_id"] == str(actor)
    assert out["actors"][0]["role"] == "actor"

    # The edge arrives "in" on this event (other → ev).
    assert out["edges"][0]["id"] == str(edge)
    assert out["edges"][0]["direction"] == "in"
    assert out["edges"][0]["src_event_id"] == str(other)

    # The tracking situation + the ledger oldest→newest (opened first).
    assert out["situations"][0]["id"] == str(sit)
    assert [row["transition"] for row in out["lifecycle"]] == [
        "opened", "advanced"]

    # Every section id is a citable ref.
    for rid in [str(ev), str(s_hi), str(s_lo), str(actor), str(edge),
                str(sit)]:
        assert rid in out["refs"]


@pytest.mark.integration
async def test_inspect_event_missing_and_malformed(port):
    miss = await port.inspect_event(event_id=str(uuid4()))
    assert miss["found"] is False
    assert "not found" in miss["error"]

    bad = await port.inspect_event(event_id="not-a-uuid")
    assert bad["found"] is False
    assert "uuid" in bad["error"]
