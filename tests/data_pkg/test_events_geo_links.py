# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V3/P1 tests — event geo derivation + the event/situation bridge.

Two reader surfaces shipped plumbed but dark (docs/DATA_MODEL_V3.md §2.7 geo,
§2.4/§6.3 the situation bridge):

  * ``events.geo_lat`` / ``geo_lon`` were unconditionally NULL — the event
    writer never derived a minted event's position from its member signals.
    :func:`legba.data.events._writes.derive_event_geo` fixes the write path;
    ``migrations/0213_events_geo_backfill.sql`` PART 1 fixes existing rows.

  * ``situation_event_links`` sat at 0 rows — nothing ever linked an event to
    the situations tracking it.
    :func:`legba.data.events._writes.link_event_to_situations` fixes the
    write path (called from ``event_clustering._write_candidate``, counted
    onto the receipt as ``funnel['situations_linked']``);
    ``migrations/0213_events_geo_backfill.sql`` PART 2 fixes existing rows.

Pure tests cover :func:`event_topic_key` (including its SQL twin). Integration
tests use ``migrated_pg``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.events import _writes as _event_writes
from legba.data.provenance import (
    AnalystContext,
    EventPayload,
    EventSignalLinkPayload,
    write_event,
)

# ---------------------------------------------------------------------------
# Pure — event_topic_key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sig,expected",
    [
        ("evt:country_watch_il|tok1,tok2#evt:il", "country_watch_il"),
        ("evt:country_watch_il#evt:il", "country_watch_il"),  # no entity tokens
        ("evt:oil price|fordow,iaea#evt:ir", "oil price"),
        ("evt:solo_topic", "solo_topic"),  # no marker at all
        ("sig:not_an_event#dim:x", None),  # wrong prefix
        ("", None),
        (None, None),
    ],
)
def test_event_topic_key(sig, expected) -> None:
    assert _event_writes.event_topic_key(sig) == expected


@pytest.mark.integration
@pytest.mark.asyncio
async def test_event_topic_key_python_and_sql_agree(pg_conn) -> None:
    """The bare topic Python extracts off an ``evt:`` signature must equal
    what :data:`LINK_EVENT_SITUATIONS_SQL`'s expression would extract off a
    same-shaped ``sig:`` signature (swap the prefix/marker, same topic) —
    the twin the migration banner claims."""
    cases = [
        "evt:country_watch_il|tok1,tok2#evt:il",
        "evt:oil price|fordow,iaea#evt:ir",
        "evt:solo_topic",
        "evt:country_g20_au|a,b,c#evt:au",
    ]
    for sig in cases:
        py_topic = _event_writes.event_topic_key(sig)
        # Recast as a sig:-shaped signature (the situation family) and run
        # the SAME split-part expression LINK_EVENT_SITUATIONS_SQL uses.
        sit_shaped = "sig:" + sig[len("evt:"):].replace("#evt:", "#dim:")
        sql_topic = await pg_conn.fetchval(
            """
            SELECT split_part(
                       regexp_replace(
                           regexp_replace($1::text, '^sig:', ''),
                           '#dim:.*$', ''
                       ),
                       '|', 1
                   )
            """,
            sit_shaped,
        )
        assert py_topic == sql_topic, f"twin diverged on {sig!r}"


# ---------------------------------------------------------------------------
# Integration — migrated_pg
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig, clean_tables):
    """A clean event/situation substrate and a short-lived pool."""
    await clean_tables(
        "events", "signals", "entity_profiles", "analyst_outputs",
        "situations", "source_descriptors", "output_dead_letter",
        "alert_trigger_watermarks",
    )
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=3)
    yield pool
    await pool.close()


def _actx(**kw) -> AnalystContext:
    base = dict(
        analyst_id=f"analyst.test_geo_links_{uuid4().hex[:8]}",
        analyst_version="p1test",
        run_id=uuid4(),
        target_id=None,
        target_version=None,
    )
    base.update(kw)
    return AnalystContext(**base)


async def _signal(
    conn,
    *,
    country: str | None,
    lat: float | None = None,
    lon: float | None = None,
    fetched_at: datetime | None = None,
) -> UUID:
    """One signal, optionally carrying a country code and/or coordinates in
    the SAME shape :mod:`legba.data.filters.geocode` writes."""
    geo_arr = [country] if country else []
    payload: dict = {}
    if lat is not None or lon is not None:
        payload["geo"] = {"lat": lat, "lon": lon, "country_iso2": country}
    return await conn.fetchval(
        "INSERT INTO signals (source_id, modality, payload, content_hash,"
        " fetched_at, geo) VALUES ('rss_main', 'text', $1::jsonb, $2, $3,"
        " $4::text[]) RETURNING id",
        json.dumps(payload),
        f"h-{uuid4().hex}",
        fetched_at or datetime.now(tz=timezone.utc),
        geo_arr,
    )


def _event_payload(sig: str, *, signals: list[EventSignalLinkPayload], **kw) -> EventPayload:
    base = dict(
        event_signature=sig,
        title="Test occurrence",
        category="conflict",
        confidence=0.7,
        signal_count=len(signals),
        distinct_source_count=1,
        source_method="tower",
        signals=signals,
    )
    base.update(kw)
    return EventPayload(**base)


# ---------------------------------------------------------------------------
# derive_event_geo — direct unit-of-work tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_derive_event_geo_majority_country_first_member(pg_conn) -> None:
    """Two 'ir' members outvote one 'us' member; the point comes from the
    EARLIEST-evidenced 'ir' member, never the 'us' one and never the later
    'ir' one."""
    t0 = datetime.now(tz=timezone.utc)
    ir_early = await _signal(
        pg_conn, country="ir", lat=35.7, lon=51.4, fetched_at=t0,
    )
    ir_late = await _signal(
        pg_conn, country="ir", lat=99.9, lon=99.9,
        fetched_at=t0 + timedelta(hours=1),
    )
    us_signal = await _signal(
        pg_conn, country="us", lat=38.9, lon=-77.0, fetched_at=t0,
    )
    geo, lat, lon = await _event_writes.derive_event_geo(
        pg_conn, [ir_early, ir_late, us_signal],
    )
    assert geo == ["ir"]
    assert lat == 35.7
    assert lon == 51.4


@pytest.mark.integration
@pytest.mark.asyncio
async def test_derive_event_geo_no_geocoded_member_stays_null(pg_conn) -> None:
    """Members carry a country but no coordinates -> geo_lat/geo_lon NULL,
    never invented (never a country centroid)."""
    s1 = await _signal(pg_conn, country="ir")
    s2 = await _signal(pg_conn, country="ir")
    geo, lat, lon = await _event_writes.derive_event_geo(pg_conn, [s1, s2])
    assert geo == ["ir"]
    assert lat is None and lon is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_derive_event_geo_no_member_geo_at_all(pg_conn) -> None:
    s1 = await _signal(pg_conn, country=None)
    geo, lat, lon = await _event_writes.derive_event_geo(pg_conn, [s1])
    assert geo == [] and lat is None and lon is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_derive_event_geo_empty_input(pg_conn) -> None:
    geo, lat, lon = await _event_writes.derive_event_geo(pg_conn, [])
    assert geo == [] and lat is None and lon is None


# ---------------------------------------------------------------------------
# write_event — geo_lat/geo_lon end to end
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_event_carries_majority_country_coordinates(
    pg_conn, monkeypatch,
) -> None:
    """An event minted from two geocoded members carries the majority
    country's coordinates (the brief's headline case)."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    t0 = datetime.now(tz=timezone.utc)
    ir1 = await _signal(pg_conn, country="ir", lat=35.7, lon=51.4, fetched_at=t0)
    ir2 = await _signal(
        pg_conn, country="ir", lat=40.0, lon=52.0,
        fetched_at=t0 + timedelta(minutes=5),
    )
    payload = _event_payload(
        "evt:reactor strike|fordow#evt:ir",
        signals=[
            EventSignalLinkPayload(signal_id=ir1, linked_at=t0),
            EventSignalLinkPayload(signal_id=ir2, linked_at=t0 + timedelta(minutes=5)),
        ],
    )
    out, dlq = await write_event(
        pg_conn, analyst_ctx=_actx(), payload=payload, derived_from=[uuid4()],
    )
    assert dlq is None and out is not None
    row = await pg_conn.fetchrow(
        "SELECT geo_lat, geo_lon FROM events WHERE id = $1", out.id,
    )
    assert row["geo_lat"] == 35.7
    assert row["geo_lon"] == 51.4


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_event_no_geocoded_member_stays_null(
    pg_conn, monkeypatch,
) -> None:
    """No member carries coordinates -> geo_lat/geo_lon stay NULL (no
    centroid fallback)."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    t0 = datetime.now(tz=timezone.utc)
    s1 = await _signal(pg_conn, country="ir", fetched_at=t0)
    payload = _event_payload(
        "evt:quiet occurrence|fordow#evt:ir",
        signals=[EventSignalLinkPayload(signal_id=s1, linked_at=t0)],
    )
    out, dlq = await write_event(
        pg_conn, analyst_ctx=_actx(), payload=payload, derived_from=[uuid4()],
    )
    assert dlq is None and out is not None
    row = await pg_conn.fetchrow(
        "SELECT geo_lat, geo_lon FROM events WHERE id = $1", out.id,
    )
    assert row["geo_lat"] is None and row["geo_lon"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_event_relink_does_not_flicker_geo_to_null(
    pg_conn, monkeypatch,
) -> None:
    """A re-materialization whose OWN payload.signals omits the earlier
    geocoded member must not wipe out the point already derived — the
    derivation unions this write's links with whatever is already linked to
    the (event_signature, analyst_id) identity."""
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    actx = _actx()
    t0 = datetime.now(tz=timezone.utc)
    geocoded = await _signal(pg_conn, country="ir", lat=35.7, lon=51.4, fetched_at=t0)
    sig = "evt:ongoing occurrence|fordow#evt:ir"
    out, dlq = await write_event(
        pg_conn,
        analyst_ctx=actx,
        payload=_event_payload(
            sig, signals=[EventSignalLinkPayload(signal_id=geocoded, linked_at=t0)],
        ),
        derived_from=[uuid4()],
    )
    assert dlq is None and out is not None
    row = await pg_conn.fetchrow(
        "SELECT geo_lat, geo_lon FROM events WHERE id = $1", out.id,
    )
    assert row["geo_lat"] == 35.7

    # Re-emit with a DIFFERENT, ungeocoded member only — the earlier
    # geocoded member is not repeated in this tick's payload.signals.
    later = t0 + timedelta(hours=1)
    ungeocoded = await _signal(pg_conn, country="ir", fetched_at=later)
    out2, dlq2 = await write_event(
        pg_conn,
        analyst_ctx=actx,
        payload=_event_payload(
            sig, signals=[EventSignalLinkPayload(signal_id=ungeocoded, linked_at=later)],
        ),
        derived_from=[uuid4()],
    )
    assert dlq2 is None and out2 is not None
    row2 = await pg_conn.fetchrow(
        "SELECT geo_lat, geo_lon FROM events WHERE id = $1", out.id,
    )
    assert row2["geo_lat"] == 35.7 and row2["geo_lon"] == 51.4


# ---------------------------------------------------------------------------
# link_event_to_situations
# ---------------------------------------------------------------------------


async def _situation(
    conn,
    *,
    sig: str,
    status: str = "active",
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO situations
            (id, data, name, status, category, event_count, intensity_score,
             produced_at, situation_signature, valid_from, valid_until)
        VALUES (gen_random_uuid(), '{}'::jsonb, $1, $2, '', 0, 0.0,
                now(), $3, $4, $5)
        RETURNING id
        """,
        f"Situation for {sig}",
        status,
        sig,
        valid_from,
        valid_until,
    )


async def _event(conn, *, sig: str, valid_from: datetime | None = None) -> UUID:
    """A minimal events row, bypassing write_event (the linker only reads
    ``events.event_signature``/``valid_from`` — it does not require a full
    write-path insert)."""
    return await conn.fetchval(
        """
        INSERT INTO events
            (id, event_signature, analyst_id, title, lifecycle_changed_at,
             valid_from)
        VALUES (gen_random_uuid(), $1,
                'analyst.test_geo_links_events', 't', now(), $2)
        RETURNING id
        """,
        sig,
        valid_from,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_link_event_to_situations_matches_open_not_closed_or_unrelated(
    pg_conn,
) -> None:
    now = datetime.now(tz=timezone.utc)
    event_id = await _event(pg_conn, sig="evt:country_watch_il|a,b#evt:il", valid_from=now)
    matching_open = await _situation(pg_conn, sig="sig:country_watch_il#dim:x")
    matching_closed = await _situation(
        pg_conn, sig="sig:country_watch_il#dim:y", status="closed",
    )
    unrelated_open = await _situation(pg_conn, sig="sig:country_watch_au#dim:x")

    n = await _event_writes.link_event_to_situations(
        pg_conn, event_id, "evt:country_watch_il|a,b#evt:il",
        derived_from=[uuid4()], window_at=now,
    )
    assert n == 1
    linked = {
        r["situation_id"] for r in await pg_conn.fetch(
            "SELECT situation_id FROM situation_event_links WHERE event_id = $1",
            event_id,
        )
    }
    assert linked == {matching_open}
    assert matching_closed not in linked
    assert unrelated_open not in linked


@pytest.mark.integration
@pytest.mark.asyncio
async def test_link_event_to_situations_respects_window(pg_conn) -> None:
    now = datetime.now(tz=timezone.utc)
    event_id = await _event(pg_conn, sig="evt:country_watch_de|a#evt:de", valid_from=now)
    in_window = await _situation(
        pg_conn, sig="sig:country_watch_de#dim:x",
        valid_from=now - timedelta(days=1), valid_until=now + timedelta(days=1),
    )
    expired = await _situation(
        pg_conn, sig="sig:country_watch_de#dim:y",
        valid_from=now - timedelta(days=10), valid_until=now - timedelta(days=5),
    )
    not_yet_open = await _situation(
        pg_conn, sig="sig:country_watch_de#dim:z",
        valid_from=now + timedelta(days=5), valid_until=None,
    )
    unbounded = await _situation(
        pg_conn, sig="sig:country_watch_de#dim:w",
        valid_from=None, valid_until=None,
    )

    n = await _event_writes.link_event_to_situations(
        pg_conn, event_id, "evt:country_watch_de|a#evt:de",
        derived_from=[uuid4()], window_at=now,
    )
    assert n == 2
    linked = {
        r["situation_id"] for r in await pg_conn.fetch(
            "SELECT situation_id FROM situation_event_links WHERE event_id = $1",
            event_id,
        )
    }
    assert linked == {in_window, unbounded}
    assert expired not in linked
    assert not_yet_open not in linked


@pytest.mark.integration
@pytest.mark.asyncio
async def test_link_event_to_situations_idempotent(pg_conn) -> None:
    now = datetime.now(tz=timezone.utc)
    event_id = await _event(pg_conn, sig="evt:country_watch_fr|a#evt:fr", valid_from=now)
    await _situation(pg_conn, sig="sig:country_watch_fr#dim:x")

    n1 = await _event_writes.link_event_to_situations(
        pg_conn, event_id, "evt:country_watch_fr|a#evt:fr",
        derived_from=[uuid4()], window_at=now,
    )
    n2 = await _event_writes.link_event_to_situations(
        pg_conn, event_id, "evt:country_watch_fr|a#evt:fr",
        derived_from=[uuid4()], window_at=now,
    )
    assert n1 == 1
    assert n2 == 0
    total = await pg_conn.fetchval(
        "SELECT count(*) FROM situation_event_links WHERE event_id = $1", event_id,
    )
    assert total == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_link_event_to_situations_ignores_explicit_sit_signature(
    pg_conn,
) -> None:
    """A 'sit:' explicit situation has no shared topic grammar — never a
    match target, even if its raw text happens to equal the event's topic."""
    now = datetime.now(tz=timezone.utc)
    event_id = await _event(pg_conn, sig="evt:x|a#evt:xx", valid_from=now)
    await _situation(pg_conn, sig="sit:x")

    n = await _event_writes.link_event_to_situations(
        pg_conn, event_id, "evt:x|a#evt:xx",
        derived_from=[uuid4()], window_at=now,
    )
    assert n == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_event_clustering_funnel_carries_situations_linked_key(
    pg_pool, monkeypatch,
) -> None:
    """The clustering receipt's funnel carries a ``situations_linked`` key
    (the count of NEW links THIS tick's writes made) on every pass, even a
    clean no-op one — the counter is always present, never conditionally
    absent, so a dashboard reading the receipt need not guard for it."""
    from legba.data.analysts.deterministic_handlers import event_clustering
    from types import SimpleNamespace

    monkeypatch.setenv("LEGBA_EVENTS", "1")
    result = await event_clustering.handle(
        [], _options(), SimpleNamespace(pg_pool=pg_pool, extras={}),
    )
    funnel = result.finding.data["funnel"]
    assert funnel["situations_linked"] == 0


def _options(**kw):
    base = {
        "sub_handler": "event_clustering",
        "analyst_id": f"analyst.event_clustering_geo_test_{uuid4().hex[:8]}",
        "analyst_version": "p1-test",
        "run_id": uuid4(),
        "lookback_hours": 2,
        "max_signals": 100,
        "include_tower": False,
        "max_lifecycle_events": 100,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Migration 0213 — applies, and its own SQL backfills correctly
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_0213_backfills_geo_and_links(pg_conn) -> None:
    """Insert legacy-shaped rows (pre-migration state: NULL geo_lat/lon, an
    event and an open situation sharing a topic, no situation_event_links
    row) and re-apply the migration's own SQL text — the file
    ``test_migrations.py`` already proves applies once at DB setup; this
    proves what it DOES when there is something to repair."""
    from pathlib import Path

    migration_sql = (
        Path(__file__).resolve().parents[2]
        / "src" / "legba" / "data" / "migrations"
        / "0213_events_geo_backfill.sql"
    ).read_text()

    t0 = datetime.now(tz=timezone.utc)
    s1 = await _signal(pg_conn, country="jp", lat=35.6, lon=139.7, fetched_at=t0)
    event_id = await _event(pg_conn, sig="evt:country_watch_jp|a#evt:jp", valid_from=t0)
    await pg_conn.execute(
        "INSERT INTO signal_event_links (signal_id, event_id, linked_at)"
        " VALUES ($1, $2, $3)",
        s1, event_id, t0,
    )
    situation_id = await _situation(pg_conn, sig="sig:country_watch_jp#dim:x")

    before = await pg_conn.fetchrow(
        "SELECT geo_lat, geo_lon FROM events WHERE id = $1", event_id,
    )
    assert before["geo_lat"] is None and before["geo_lon"] is None
    assert await pg_conn.fetchval(
        "SELECT count(*) FROM situation_event_links WHERE event_id = $1",
        event_id,
    ) == 0

    await pg_conn.execute(migration_sql)

    after = await pg_conn.fetchrow(
        "SELECT geo_lat, geo_lon FROM events WHERE id = $1", event_id,
    )
    assert after["geo_lat"] == 35.6
    assert after["geo_lon"] == 139.7
    linked = await pg_conn.fetchval(
        "SELECT count(*) FROM situation_event_links"
        " WHERE event_id = $1 AND situation_id = $2",
        event_id, situation_id,
    )
    assert linked == 1

    # Re-applying again is a no-op (idempotent) — no error, no duplicate.
    await pg_conn.execute(migration_sql)
    linked_again = await pg_conn.fetchval(
        "SELECT count(*) FROM situation_event_links"
        " WHERE event_id = $1 AND situation_id = $2",
        event_id, situation_id,
    )
    assert linked_again == 1
