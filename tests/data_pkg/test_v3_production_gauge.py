# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/system/production-gauge`` — the S-1 whole-engine read surface.

The route is a thin projection of :mod:`legba.data.registry.production_gauge`,
which the ``production_deficit`` alert-trigger class also reads. That sharing
is the property these tests protect: there is no mirrored SQL here to drift,
and the ``pages`` field on every row is the SAME predicate the alert plane
uses, so the table and the operator's phone are visibly one instrument.

The rest holds the route to the family's honesty bar:

  * ``ungauged`` is never folded into "ok" — the totals report ``gauged`` and
    ``ungauged`` separately, because "we cannot say" and "it is fine" are
    different statements and a single health percentage would hide the
    difference;
  * ``ratio`` is ``null`` exactly when a loop is ungauged, so no reader can
    mistake "no expectation" for a measured 0.0;
  * ``totals`` is computed over the FULL read before any filter, so a
    ``?deficits_only=true`` call cannot lie about its denominator;
  * an unreadable substrate degrades to an honest empty payload at HTTP 200
    with ``measured: false`` — an empty table that says it measured nothing,
    never an empty table that reads as all-clear.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry import production_gauge, production_gauge_integrity
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import MASTER_KEY_ENV, CredentialVault
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.production_gauge_api import build_production_gauge_router
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "66" * 32)

#: The INTEGRITY loops (judge availability, descriptor prompt drift) each emit
#: exactly one row per read, whatever their state — so every whole-engine total
#: below carries them. Derived, never hardcoded.
_N_INTEGRITY = len(production_gauge_integrity.INTEGRITY_LOOP_CLASSES)

#: The METERING loops (#21/#22): llm_latency emits exactly one row per read
#: (ungauged/no_calls_in_window on a blank engine); llm_daily_burn is DYNAMIC —
#: one row per component with a declared ceiling or money moving, so a blank
#: engine contributes zero. Hence exactly one fixed row from the family.
_N_METERING_FIXED = 1

_ROUTE = "/api/v1/v3/system/production-gauge"
_SRC = "source.gaugeroute.frozen"


# ---------------------------------------------------------------------------
# Pure — registration + contract, no DB
# ---------------------------------------------------------------------------


def test_route_is_registered():
    router = build_production_gauge_router(deps=object())  # type: ignore[arg-type]
    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    assert "/system/production-gauge" in paths


def test_empty_payload_is_honestly_unmeasured():
    """The degraded return must be distinguishable from a healthy engine."""
    from legba.data.registry.production_gauge_api import ProductionGaugeOut

    out = ProductionGaugeOut()
    assert out.measured is False
    assert out.loops == []
    assert out.totals.gauged == 0
    assert out.totals.deficit == 0


def test_alert_floor_is_published_on_the_wire():
    """A reader must be able to see WHICH rows would page without guessing the
    alert plane's private threshold."""
    from legba.data.registry.production_gauge_api import ProductionGaugeOut

    assert ProductionGaugeOut().alert_min_severity == (
        production_gauge.ALERT_MIN_SEVERITY
    )


# ---------------------------------------------------------------------------
# Rig
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    identity = SigningIdentity(
        signing_key=SigningKey(b"s1-production-gauge-route-seed-01"[:32]),
        signer_did="did:legba:registry:s1-production-gauge-test",
    )
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)
    descriptor_registry = DescriptorRegistry(
        pg_store,
        vocabulary_cache=vocab,
        signing_identity=identity,
        audit_logger=audit,
        dead_letter=dlq,
    )
    await descriptor_registry.start()
    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry,
        stack_registry=StackRegistry(pg_store, vault, audit=audit, dlq=dlq),
        vault=vault,
        dlq=dlq,
        audit_logger=audit,
        vocabulary_cache=vocab,
        nats_store=None,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_production_gauge_router(deps), prefix="/api/v1/v3")

    yield app, pg_store

    await descriptor_registry.stop()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(api_app):
    app, _ = api_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# `blank` — an empty engine for THIS test, without wrecking the session.
# ---------------------------------------------------------------------------
# The gauge is a WHOLE-ENGINE measurement. Its assertions are about totals
# ("totals are computed before filters", "an empty engine reads measured but
# empty"), so these tests genuinely need the substrate empty — not merely free
# of the rows they inserted themselves. That is why this fixture truncates
# instead of deleting what it wrote.
#
# The cost of that truncation used to be paid by whoever ran NEXT. It is
# global and permanent, and tests/data_pkg/test_claim_watch.py's global
# hub-damping tests read document frequency across the WHOLE `signals` stream:
# emptying it underneath them moved their denominator, and
# `test_stream_hub_entities_are_floored_and_specific_ones_untouched` went from
# 3 down-weighted entities to 6. Nobody saw it, because in file order this
# module sorts AFTER test_claim_watch.py — so the bug was invisible right up
# until something shuffled the suite, at which point it is a coin flip. A
# suite that only passes in one order is a suite whose green means nothing,
# which is exactly what the nightly's shuffled pass exists to find.
#
# So: snapshot what the truncation will destroy, hand the test a genuinely
# empty engine, and put the substrate back exactly as it was on the way out.
# The tables involved hold ~1.6k rows on the dev rig, so the copy is free.

#: The tables the gauge needs emptied.
_BLANK_TRUNCATES = (
    "analyst_descriptors",
    "source_descriptors",
    "analyst_traces",
    "analyst_outputs",
    "signals",
    "source_poll_outcomes",
    "acute_forecasts",
)

#: `TRUNCATE ... CASCADE` silently empties every table holding an FK into the
#: list above, so the restore has to cover those too — putting back only the
#: named seven would leave these three permanently empty for the rest of the
#: session, which is the same bug in a quieter costume. Children are restored
#: LAST so the references they carry have rows to point at.
_BLANK_CASCADE_TAKES = (
    "alert_sink_deliveries",   # -> analyst_outputs
    "analyst_critiques",       # -> analyst_traces
    "output_dead_letter",      # -> analyst_traces
)

_BACKUP_SCHEMA = "_gauge_blank_backup"

#: The FK closure is asserted, not assumed. A migration that adds a new FK into
#: any truncated table would silently widen the CASCADE and this fixture would
#: go back to destroying state it does not restore — so recompute the closure
#: at runtime and fail LOUDLY instead of leaving the next test to fail weirdly.
_CLOSURE_SQL = """
WITH RECURSIVE seed(t) AS (
    SELECT unnest($1::text[])
), closure(t) AS (
    SELECT t FROM seed
    UNION
    SELECT c.conrelid::regclass::text
      FROM pg_constraint c
      JOIN closure cl ON c.confrelid::regclass::text = cl.t
     WHERE c.contype = 'f'
)
SELECT t FROM closure
"""


@pytest_asyncio.fixture
async def blank(api_app):
    _, pg_store = api_app
    parents = list(_BLANK_TRUNCATES)
    children = list(_BLANK_CASCADE_TAKES)

    async with pg_store.acquire() as conn:
        actual = {r["t"] for r in await conn.fetch(_CLOSURE_SQL, parents)}
        declared = set(parents) | set(children)
        assert actual == declared, (
            "the TRUNCATE ... CASCADE closure changed: "
            f"{sorted(actual ^ declared)}. A new foreign key means this "
            "fixture now destroys a table it does not restore — add it to "
            "_BLANK_CASCADE_TAKES (children restore last) before this file "
            "starts corrupting whatever runs after it."
        )

        # A previous run that died between truncate and restore would have
        # left the schema behind; that stale copy must never be restored over
        # live rows.
        await conn.execute(f"DROP SCHEMA IF EXISTS {_BACKUP_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_BACKUP_SCHEMA}")
        for table in parents + children:
            await conn.execute(
                f"CREATE TABLE {_BACKUP_SCHEMA}.{table} AS TABLE public.{table}"
            )
        await conn.execute(
            f"TRUNCATE {', '.join(parents)} RESTART IDENTITY CASCADE"
        )

    try:
        yield
    finally:
        # Unconditional: a failing assertion inside the test must not be the
        # reason the next file sees an empty substrate.
        async with pg_store.acquire() as conn:
            await conn.execute(
                f"TRUNCATE {', '.join(parents)} RESTART IDENTITY CASCADE"
            )
            for table in parents + children:
                await conn.execute(
                    f"INSERT INTO public.{table} "
                    f"SELECT * FROM {_BACKUP_SCHEMA}.{table}"
                )
            await conn.execute(f"DROP SCHEMA {_BACKUP_SCHEMA} CASCADE")


async def _seed_frozen_source(conn) -> None:
    body = {
        "identity": {"id": _SRC, "kind": "rss", "state": "active"},
        "acquisition": "poll",
        "cadence": {"schedule": {"raw": "13 * * * *", "ui_hint": {}}},
    }
    await conn.execute(
        """
        INSERT INTO source_descriptors
            (descriptor_id, version, schema_uri, is_head, kind, state, owner,
             name, body, created_at)
        VALUES ($1, 'v1', 'legba/source/1.0.0', TRUE, 'rss', 'active', 'test',
                $1, $2::jsonb, now() - interval '60 days')
        """,
        _SRC,
        json.dumps(body),
    )
    for i in range(20):
        await conn.execute(
            """
            INSERT INTO signals
                (id, source_id, source_version, payload, content_hash,
                 fetched_at, created_at, updated_at, schema_uri)
            VALUES ($1, $2, 'v1', '{}'::jsonb, $3, now(),
                    now() - make_interval(hours => $4), now(),
                    'iglu:legba/signal/jsonschema/1-0-0')
            """,
            uuid4(),
            _SRC,
            uuid4().hex,
            192 + i,
        )
    for h in range(120):
        await conn.execute(
            """
            INSERT INTO source_poll_outcomes
                (source_id, source_version, outcome, health_state,
                 signals_written, occurred_at)
            VALUES ($1, 'v1', 'success', 'healthy', 0,
                    now() - make_interval(hours => $2))
            """,
            _SRC,
            h,
        )


async def _seed_paused_source(conn) -> None:
    body = {
        "identity": {"id": "source.gaugeroute.paused", "kind": "rss"},
        "acquisition": "poll",
        "cadence": {"schedule": {"raw": "13 * * * *", "ui_hint": {}}},
    }
    await conn.execute(
        """
        INSERT INTO source_descriptors
            (descriptor_id, version, schema_uri, is_head, kind, state, owner,
             name, body, created_at)
        VALUES ($1, 'v1', 'legba/source/1.0.0', TRUE, 'rss', 'paused', 'test',
                $1, $2::jsonb, now() - interval '60 days')
        """,
        "source.gaugeroute.paused",
        json.dumps(body),
    )


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gauge_serves_the_deficit_worst_first(api_app, client, blank):
    _, pg_store = api_app
    async with pg_store.acquire() as conn:
        await _seed_frozen_source(conn)
        await _seed_paused_source(conn)

    r = await client.get(_ROUTE)
    assert r.status_code == 200
    body = r.json()

    assert body["measured"] is True
    assert body["window_days"] == production_gauge.GaugeConfig().window_days
    assert body["totals"]["deficit"] == 1
    assert body["totals"]["paging"] == 1
    # The paused source, plus EVERY declared backlog drain — each reports
    # no_overdue_work against its own empty table on a blank engine — plus the
    # INTEGRITY loops, which on a blank engine have no critiques and no live
    # descriptor prompts and so report their own quiet reasons. Derived from the
    # registries rather than hardcoded so declaring a new backlog or a new
    # integrity loop is six lines and a test run (its stated cost), not a
    # route-test edit.
    assert body["totals"]["ungauged"] == (
        1 + len(production_gauge.BACKLOG_DRAINS) + _N_INTEGRITY + _N_METERING_FIXED
    )
    assert body["totals"]["by_class"]["backlog_drain"]["ungauged"] == len(
        production_gauge.BACKLOG_DRAINS
    )

    top = body["loops"][0]
    assert top["loop_id"] == _SRC
    assert top["loop_class"] == "source_production"
    assert top["state"] == "deficit"
    assert top["severity"] == "critical"
    assert top["pages"] is True
    assert top["expected"] and top["actual"]
    assert top["evidence"]["polls_ok"] == 120


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ungauged_rows_carry_a_reason_and_no_ratio(api_app, client, blank):
    """A paused source is not healthy and not broken — it is unmeasurable, and
    the wire must say which."""
    _, pg_store = api_app
    async with pg_store.acquire() as conn:
        await _seed_paused_source(conn)

    body = (await client.get(_ROUTE)).json()
    row = next(g for g in body["loops"] if g["loop_id"] == "source.gaugeroute.paused")
    assert row["state"] == "ungauged"
    assert row["quiet_reason"] == production_gauge.QUIET_NOT_ACTIVE
    assert row["ratio"] is None
    assert row["pages"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_totals_are_computed_before_filters(api_app, client, blank):
    """``?deficits_only=true`` returning ``loops: 1`` alongside ``ok: 0`` would
    be lying about the denominator — the roll-up is always engine-wide."""
    _, pg_store = api_app
    async with pg_store.acquire() as conn:
        await _seed_frozen_source(conn)
        await _seed_paused_source(conn)

    full = (await client.get(_ROUTE)).json()
    filtered = (await client.get(_ROUTE, params={"deficits_only": True})).json()

    assert len(filtered["loops"]) == 1
    assert filtered["totals"] == full["totals"]
    assert filtered["totals"]["ungauged"] == (
        1 + len(production_gauge.BACKLOG_DRAINS) + _N_INTEGRITY + _N_METERING_FIXED
    )
    assert filtered["totals"]["loops"] > len(filtered["loops"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_paging_only_matches_the_alert_planes_own_predicate(
    api_app, client, blank
):
    """The instrument-identity property: what the table calls 'would page' is
    the same set the production_deficit trigger class would raise."""
    _, pg_store = api_app
    async with pg_store.acquire() as conn:
        await _seed_frozen_source(conn)
        report = await production_gauge.read_gauge(conn)

    body = (await client.get(_ROUTE, params={"paging_only": True})).json()
    assert {g["loop_id"] for g in body["loops"]} == {
        g.loop_id for g in report.loops if g.pages
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_class_and_state_filters(api_app, client, blank):
    _, pg_store = api_app
    async with pg_store.acquire() as conn:
        await _seed_frozen_source(conn)
        await _seed_paused_source(conn)

    by_class = (
        await client.get(_ROUTE, params={"loop_class": "source_production"})
    ).json()
    assert by_class["loops"]
    assert {g["loop_class"] for g in by_class["loops"]} == {"source_production"}

    by_state = (await client.get(_ROUTE, params={"state": "ungauged"})).json()
    assert {g["state"] for g in by_state["loops"]} == {"ungauged"}

    none_match = (
        await client.get(_ROUTE, params={"loop_class": "backlog_drain",
                                         "state": "deficit"})
    ).json()
    assert none_match["loops"] == []
    # ...and the totals still tell the truth about the engine.
    assert none_match["totals"]["deficit"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_window_days_override_changes_the_baseline(api_app, client, blank):
    """Narrowing the window drops the source's own production history out of
    view, so it stops having a gap baseline and becomes honestly ungauged
    rather than silently 'ok'."""
    _, pg_store = api_app
    async with pg_store.acquire() as conn:
        await _seed_frozen_source(conn)

    wide = (await client.get(_ROUTE)).json()
    narrow = (await client.get(_ROUTE, params={"window_days": 2})).json()

    assert wide["window_days"] == 21
    assert narrow["window_days"] == 2
    wide_row = next(g for g in wide["loops"] if g["loop_id"] == _SRC)
    narrow_row = next(g for g in narrow["loops"] if g["loop_id"] == _SRC)
    assert wide_row["state"] == "deficit"
    # Zero signals inside 2 days + plenty of healthy polls = the silent branch.
    assert narrow_row["state"] == "deficit"
    assert narrow_row["evidence"]["sub_state"] == "silent"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_engine_reads_measured_but_empty(api_app, client, blank):
    """Nothing to gauge is a real answer and must not look like a failure."""
    body = (await client.get(_ROUTE)).json()
    assert body["measured"] is True
    assert body["totals"]["gauged"] == 0
    assert body["totals"]["deficit"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_limit_bounds_the_table_without_touching_the_totals(
    api_app, client, blank
):
    _, pg_store = api_app
    async with pg_store.acquire() as conn:
        await _seed_frozen_source(conn)
        await _seed_paused_source(conn)

    body = (await client.get(_ROUTE, params={"limit": 1})).json()
    assert len(body["loops"]) == 1
    # Two sources + every declared backlog drain + the integrity loops.
    assert body["totals"]["loops"] == (
        2 + len(production_gauge.BACKLOG_DRAINS) + _N_INTEGRITY + _N_METERING_FIXED
    )
