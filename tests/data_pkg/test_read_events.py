# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""D2e — read telemetry (migration 0189 + ``/api/v1/read-events``).

The oracle wager's instrument, tested on both halves.

Pure tests cover the per-event validator (clock skew both directions, the
subject pair rule, blank guards) without a database or a clock freeze.

Ephemeral-DB tests use the ``migrated_pg`` fixture — the real migration runner
against a throwaway ``legba_test_<uuid>`` database, never the live DB — and
drive the REAL router over ``httpx`` ``ASGITransport``, so every assertion
traverses the binding path an operator's browser will: pydantic validation,
``require_bearer``, the multi-row INSERT, the 0189 CHECK constraints, and the
Postgres-side rollup aggregation.

The append-only guard gets its own tests because it is load-bearing for the
wager: this ledger grades the operator, and the operator owns the database,
so "nobody could have retouched it" has to be a property of the schema rather
than a promise.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from legba.data.config import PostgresConfig
from legba.data.registry.read_events_api import (
    READ_EVENT_KINDS,
    ReadEventIn,
    build_read_events_router,
    validate_event,
)

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)


def _ev(**over) -> ReadEventIn:
    base = {
        "occurred_at": NOW,
        "event_kind": "panel_open",
        "workspace": "morning_read",
        "session_nonce": "nonce-a",
    }
    base.update(over)
    return ReadEventIn(**base)


# ---------------------------------------------------------------------------
# Pure — the per-event validator
# ---------------------------------------------------------------------------


def test_validate_event_accepts_a_plain_event():
    assert validate_event(_ev(), now=NOW) is None


def test_validate_event_accepts_a_whole_subject():
    ok = _ev(event_kind="finding_open", subject_kind="finding", subject_id="f-1")
    assert validate_event(ok, now=NOW) is None


def test_validate_event_rejects_half_a_subject():
    half = _ev(subject_kind="finding")
    assert "together" in (validate_event(half, now=NOW) or "")
    other_half = _ev(subject_id="f-1")
    assert "together" in (validate_event(other_half, now=NOW) or "")


def test_validate_event_rejects_naive_timestamps():
    naive = _ev(occurred_at=datetime(2026, 8, 29, 12, 0, 0))
    assert "timezone-aware" in (validate_event(naive, now=NOW) or "")


def test_validate_event_rejects_future_skew_but_tolerates_a_little():
    tolerable = _ev(occurred_at=NOW + timedelta(minutes=30))
    assert validate_event(tolerable, now=NOW) is None
    skewed = _ev(occurred_at=NOW + timedelta(days=2))
    assert "future" in (validate_event(skewed, now=NOW) or "")


def test_validate_event_rejects_a_stale_replay():
    stale = _ev(occurred_at=NOW - timedelta(days=30))
    assert "replay" in (validate_event(stale, now=NOW) or "")
    recent = _ev(occurred_at=NOW - timedelta(days=2))
    assert validate_event(recent, now=NOW) is None


def test_validate_event_rejects_blank_workspace_and_nonce():
    assert "workspace" in (validate_event(_ev(workspace="   "), now=NOW) or "")
    assert "session_nonce" in (
        validate_event(_ev(session_nonce="  "), now=NOW) or ""
    )


def test_kind_vocabulary_is_the_eight_the_wager_names():
    assert set(READ_EVENT_KINDS) == {
        "panel_open",
        "workspace_open",
        "finding_open",
        "lineage_walk",
        "citation_drill",
        "consult_open",
        "brief_read",
    }


# ---------------------------------------------------------------------------
# Ephemeral DB — real migration, real router
# ---------------------------------------------------------------------------


class _Reg:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pg = pool


class _RouteDeps:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.descriptor_registry = _Reg(pool)


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    # TRUNCATE, not DELETE: the 0189 forbid-mutation trigger is row-level, so
    # DELETE fails loud (asserted below) while TRUNCATE — a DDL-ish whole-table
    # reset no application path ever takes — still lets tests start empty.
    async with pg_pool.acquire() as conn:
        await conn.execute("TRUNCATE read_events")


@pytest_asyncio.fixture
async def re_client(pg_pool, monkeypatch):
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)
    monkeypatch.setenv("LEGBA_DEV_MODE", "1")
    app = FastAPI()
    app.include_router(
        build_read_events_router(_RouteDeps(pg_pool)), prefix="/api/v1"
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


def _wire(**over) -> dict:
    body = {
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "event_kind": "panel_open",
        "workspace": "morning_read",
        "session_nonce": "nonce-a",
    }
    body.update(over)
    return body


async def test_migration_0189_created_the_table_and_its_indexes(pg_pool):
    """The migration ran through the real runner, not a hand-applied DDL."""
    async with pg_pool.acquire() as conn:
        applied = await conn.fetchval(
            "SELECT 1 FROM legba_data_migrations WHERE name = $1",
            "0189_read_events.sql",
        )
        assert applied == 1, "0189 did not register in the migration ledger"

        cols = {
            r["column_name"]: r
            for r in await conn.fetch(
                "SELECT column_name, is_nullable, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='read_events'"
            )
        }
        assert set(cols) == {
            "id",
            "occurred_at",
            "received_at",
            "event_kind",
            "subject_kind",
            "subject_id",
            "workspace",
            "session_nonce",
            "dwell_ms",
        }
        # subject_* are the nullable pair; the rest of the spine is NOT NULL.
        assert cols["subject_kind"]["is_nullable"] == "YES"
        assert cols["subject_id"]["is_nullable"] == "YES"
        assert cols["dwell_ms"]["is_nullable"] == "YES"
        for required in ("occurred_at", "event_kind", "workspace", "session_nonce"):
            assert cols[required]["is_nullable"] == "NO"
        # subject_id is text on purpose — findings are uuids, panels are kinds.
        assert cols["subject_id"]["data_type"] == "text"

        idx = {
            r["indexname"]
            for r in await conn.fetch(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname='public' AND tablename='read_events'"
            )
        }
        assert {
            "idx_read_events_occurred_kind",
            "idx_read_events_brief_read",
            "idx_read_events_session",
            "idx_read_events_subject",
        } <= idx


async def test_ingest_writes_a_batch_and_returns_202(re_client, pg_pool, clean_slate):
    r = await re_client.post(
        "/api/v1/read-events",
        json={
            "events": [
                _wire(event_kind="brief_read", workspace="morning_read"),
                _wire(
                    event_kind="finding_open",
                    subject_kind="finding",
                    subject_id="7f6c0b1e-0000-4000-8000-000000000001",
                    dwell_ms=4200,
                ),
                _wire(event_kind="workspace_open", workspace="desk"),
            ]
        },
    )
    assert r.status_code == 202, r.text
    assert r.json() == {"accepted": 3, "rejected": 0, "reasons": []}

    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_kind, workspace, subject_kind, subject_id, dwell_ms, "
            "received_at IS NOT NULL AS stamped FROM read_events "
            "ORDER BY event_kind"
        )
    assert [r["event_kind"] for r in rows] == [
        "brief_read",
        "finding_open",
        "workspace_open",
    ]
    finding = rows[1]
    assert finding["subject_kind"] == "finding"
    assert finding["dwell_ms"] == 4200
    assert all(r["stamped"] for r in rows), "received_at default did not fire"


async def test_ingest_drops_bad_events_but_keeps_the_good_ones(
    re_client, pg_pool, clean_slate
):
    """The documented partial-batch exception: one bad event never costs a morning."""
    future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    r = await re_client.post(
        "/api/v1/read-events",
        json={
            "events": [
                _wire(event_kind="brief_read"),
                _wire(occurred_at=future),          # clock skew
                _wire(subject_kind="finding"),      # half a subject
                _wire(event_kind="citation_drill",
                      subject_kind="signal", subject_id="s-9"),
            ]
        },
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["accepted"] == 2
    assert body["rejected"] == 2
    # Reasons are de-duplicated and stated, never silent.
    assert len(body["reasons"]) == 2
    assert any("future" in reason for reason in body["reasons"])
    assert any("together" in reason for reason in body["reasons"])

    async with pg_pool.acquire() as conn:
        kinds = [
            r["event_kind"]
            for r in await conn.fetch(
                "SELECT event_kind FROM read_events ORDER BY event_kind"
            )
        ]
    assert kinds == ["brief_read", "citation_drill"]


async def test_ingest_422s_an_unknown_kind(re_client, clean_slate):
    """A kind outside the closed vocabulary is a 422, not a ninth kind."""
    r = await re_client.post(
        "/api/v1/read-events",
        json={"events": [_wire(event_kind="panel_hover")]},
    )
    assert r.status_code == 422, r.text


async def test_ingest_rejects_an_empty_or_oversized_batch(re_client, clean_slate):
    empty = await re_client.post("/api/v1/read-events", json={"events": []})
    assert empty.status_code == 422
    huge = await re_client.post(
        "/api/v1/read-events", json={"events": [_wire() for _ in range(501)]}
    )
    assert huge.status_code == 422


async def test_read_events_are_append_only(pg_pool, clean_slate):
    """The wager's evidence base must not be retouchable by the measured party."""
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO read_events (occurred_at, event_kind, workspace, "
            "session_nonce) VALUES (now(), 'brief_read', 'morning_read', 'n1')"
        )
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await conn.execute("DELETE FROM read_events")
        with pytest.raises(asyncpg.PostgresError, match="append-only"):
            await conn.execute("UPDATE read_events SET event_kind = 'panel_open'")
        assert await conn.fetchval("SELECT count(*) FROM read_events") == 1


async def test_schema_refuses_half_a_subject_and_a_bad_kind(pg_pool, clean_slate):
    """Defence in depth: the API validator is mirrored by real CHECK constraints."""
    async with pg_pool.acquire() as conn:
        with pytest.raises(asyncpg.PostgresError, match="read_events_subject_pair"):
            await conn.execute(
                "INSERT INTO read_events (occurred_at, event_kind, workspace, "
                "session_nonce, subject_kind) VALUES "
                "(now(), 'finding_open', 'desk', 'n1', 'finding')"
            )
        with pytest.raises(asyncpg.PostgresError, match="read_events_kind_vocab"):
            await conn.execute(
                "INSERT INTO read_events (occurred_at, event_kind, workspace, "
                "session_nonce) VALUES (now(), 'panel_hover', 'desk', 'n1')"
            )
        with pytest.raises(
            asyncpg.PostgresError, match="read_events_not_far_future"
        ):
            await conn.execute(
                "INSERT INTO read_events (occurred_at, event_kind, workspace, "
                "session_nonce) VALUES "
                "(now() + interval '5 days', 'brief_read', 'desk', 'n1')"
            )


async def test_rollup_counts_by_kind_by_day_with_the_wager_scalars(
    re_client, pg_pool, clean_slate
):
    now = datetime.now(timezone.utc)
    yesterday = now - timedelta(days=1)
    four_days = now - timedelta(days=4)

    payload = [
        # Today: one brief read, two finding opens, two sessions.
        _wire(event_kind="brief_read", session_nonce="s-today"),
        _wire(
            event_kind="finding_open",
            session_nonce="s-today",
            subject_kind="finding",
            subject_id="f-1",
        ),
        _wire(
            event_kind="finding_open",
            session_nonce="s-today-2",
            subject_kind="finding",
            subject_id="f-2",
        ),
        # Yesterday: a brief read + a citation drill.
        _wire(
            occurred_at=yesterday.isoformat(),
            event_kind="brief_read",
            session_nonce="s-yday",
        ),
        _wire(
            occurred_at=yesterday.isoformat(),
            event_kind="citation_drill",
            session_nonce="s-yday",
            subject_kind="signal",
            subject_id="sig-1",
        ),
        # Four days back: a lone panel open, no brief read that day.
        _wire(
            occurred_at=four_days.isoformat(),
            event_kind="panel_open",
            session_nonce="s-old",
            subject_kind="panel",
            subject_id="system.wall",
        ),
    ]
    r = await re_client.post("/api/v1/read-events", json={"events": payload})
    assert r.status_code == 202, r.text
    assert r.json()["accepted"] == 6

    r = await re_client.get("/api/v1/read-events/rollup?days=30")
    assert r.status_code == 200, r.text
    out = r.json()

    totals = out["totals"]
    assert totals["window_days"] == 30
    assert totals["reads_today"] == 3
    assert totals["brief_reads_today"] == 1
    assert totals["reads_this_week"] == 6
    assert totals["brief_reads_this_week"] == 2
    # The headline: brief reads landed on 2 of the covered days, and the
    # operator was present on 3 — the exact ratio day 90 is graded on.
    assert totals["brief_read_days"] == 2
    assert totals["active_days"] == 3
    assert totals["sessions_this_week"] == 4

    cells = {(d["day"], d["event_kind"]): d for d in out["days"]}
    today_key = (now.date().isoformat(), "finding_open")
    assert cells[today_key]["events"] == 2
    assert cells[today_key]["sessions"] == 2
    assert cells[(now.date().isoformat(), "brief_read")]["events"] == 1
    assert cells[(four_days.date().isoformat(), "panel_open")]["events"] == 1
    # Days outside the window would not be here; nothing was seeded outside it.
    assert len(out["days"]) == 5


async def test_rollup_window_is_bounded_and_excludes_older_events(
    re_client, pg_pool, clean_slate
):
    """A short window must not silently include the whole log."""
    old = datetime.now(timezone.utc) - timedelta(days=5)
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO read_events (occurred_at, event_kind, workspace, "
            "session_nonce) VALUES ($1, 'brief_read', 'morning_read', 'n-old')",
            old,
        )
        await conn.execute(
            "INSERT INTO read_events (occurred_at, event_kind, workspace, "
            "session_nonce) VALUES (now(), 'brief_read', 'morning_read', 'n-new')"
        )

    narrow = (await re_client.get("/api/v1/read-events/rollup?days=2")).json()
    assert narrow["totals"]["brief_read_days"] == 1
    assert len(narrow["days"]) == 1

    wide = (await re_client.get("/api/v1/read-events/rollup?days=30")).json()
    assert wide["totals"]["brief_read_days"] == 2
    assert len(wide["days"]) == 2

    # The window is bounded on both ends — no unbounded scans on request.
    assert (await re_client.get("/api/v1/read-events/rollup?days=0")).status_code == 422
    assert (
        await re_client.get("/api/v1/read-events/rollup?days=999")
    ).status_code == 422


async def test_rollup_of_an_empty_log_is_zeroes_not_an_error(
    re_client, clean_slate
):
    """Day one — and the honest answer to 'has the operator read anything?'."""
    r = await re_client.get("/api/v1/read-events/rollup")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["days"] == []
    assert out["totals"]["reads_today"] == 0
    assert out["totals"]["brief_read_days"] == 0
    assert out["totals"]["window_days"] == 30


async def test_endpoints_are_bearer_gated(pg_pool, monkeypatch, clean_slate):
    """The registry's single auth gate applies to both halves of the plane."""
    monkeypatch.setenv("LEGBA_REGISTRY_API_TOKEN", "s3cret")
    monkeypatch.delenv("LEGBA_DEV_MODE", raising=False)
    app = FastAPI()
    app.include_router(
        build_read_events_router(_RouteDeps(pg_pool)), prefix="/api/v1"
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (
            await c.post("/api/v1/read-events", json={"events": [_wire()]})
        ).status_code == 401
        assert (await c.get("/api/v1/read-events/rollup")).status_code == 401
        ok = await c.post(
            "/api/v1/read-events",
            json={"events": [_wire()]},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert ok.status_code == 202, ok.text


def test_router_is_mounted_on_the_real_app(migrated_pg: PostgresConfig):
    """The binding path: both routes exist on the app ``create_app`` builds.

    A router that only ever gets mounted by its own test is a router the
    deployed process does not have. This asserts the real ``create_app``
    wiring, not a hand-mounted ``FastAPI()`` — the same reason the healthz
    readiness suite builds the real app.

    READ THE PATHS OFF THE OPENAPI SPEC, NOT ``app.routes``. FastAPI changed
    the shape of ``app.routes`` in the 0.137→0.140 line: ``include_router``
    no longer FLATTENS a child router's routes into the parent's list, it
    appends one opaque ``_IncludedRouter`` marker per include (a ``BaseRoute``
    with no ``.path``). Under the old shape a naive
    ``{r.path for r in app.routes}`` saw every leaf route; under the new one
    it sees four (openapi.json + the two app-level ``@app.get`` decorators +
    ``None``) no matter how many routers are mounted, so the naive form
    "passes" only on an interpreter pinned below the change. The host runs
    fastapi 0.136.0; the runtime image (and therefore the release gate's
    in-container run, and production) runs 0.140+ — which is exactly how this
    assertion could be green on the host and red in the gate while the routes
    themselves were mounted and serving the whole time. ``app.openapi()``
    is public API, is derived from the real (effective) route table, and
    answers the same question identically on both shapes.
    """
    import os

    from legba.data.config import NatsConfig
    from legba.data.registry.credentials import MASTER_KEY_ENV
    from legba.data.registry.server import create_app

    os.environ.setdefault(MASTER_KEY_ENV, "00112233445566778899" * 3 + "0011")
    os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "22" * 32)

    app = create_app(
        pg_config=migrated_pg,
        nats_config=NatsConfig.from_env(),
        enable_healthcheck_loop=False,
        enable_vocabulary_subscription=False,
    )
    paths = set(app.openapi()["paths"])
    assert "/api/v1/read-events" in paths
    assert "/api/v1/read-events/rollup" in paths
    # The spec is only a faithful proxy for the route table if it is actually
    # populated — a stub/empty spec would make the two asserts above vacuous.
    assert len(paths) > 50, sorted(paths)
