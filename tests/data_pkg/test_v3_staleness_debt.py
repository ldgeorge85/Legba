# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/system/staleness-debt`` — the first read route for the KW-3 gauge.

Before this route the ``claim_watch`` matcher's ``staleness_debt`` existed ONLY
inside the producing run's ``analyst_traces`` receipt (SEAMS #49), so reading
it meant reading a receipt. These tests hold the route to the bar that makes
exposing it safe:

  * the headline number is the MATCHER'S OWN SQL (a byte-equality drift guard
    against ``claim_watch._STALENESS_DEBT_SQL``) — route and receipt can never
    publish different numbers;
  * the same three exclusions the matcher applies hold end-to-end through HTTP
    (closed flags out, superseded consumers out, live consumers in);
  * the honesty contract survives the exposure — ``match_verified`` is hard
    ``False``, and the count of flags on superseded consumers is reported
    separately rather than folded in;
  * an unreadable/absent flag plane degrades to an honest all-zero payload at
    HTTP 200, never a 500 the panel polls.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.analysts.deterministic_handlers import claim_watch as cw
from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry import v3_api
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.v3_api import build_v3_router
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "66" * 32)

_ROUTE = "/api/v1/v3/system/staleness-debt"


# ---------------------------------------------------------------------------
# The drift guard — the reason this route can be trusted at all
# ---------------------------------------------------------------------------


def test_staleness_debt_sql_mirrors_matcher():
    """The registry's local copy is BYTE-identical to the matcher's constant.

    The registry deliberately does not import the handler package (the
    registry-slim rule — see the mirror comment in ``v3_api``), so the SQL is
    duplicated. That is only safe with this guard: if either side is edited
    alone, the route would publish a number the receipt does not, and this
    fails first.
    """
    assert v3_api._STALENESS_DEBT_SQL == cw._STALENESS_DEBT_SQL


def test_route_reports_the_matcher_analyst_id():
    """The id the gauge's last-run timestamp is looked up under must be the id
    the matcher actually writes traces as."""
    assert v3_api._CLAIM_WATCH_ANALYST_ID == cw.SUB_HANDLER_NAME


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    identity = SigningIdentity(
        signing_key=SigningKey(b"kw3-staleness-debt-route-seed-01"[:32]),
        signer_did="did:legba:registry:kw3-staleness-debt-test",
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
    app.include_router(build_v3_router(deps), prefix="/api/v1/v3")

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


@pytest_asyncio.fixture
async def clean_flags(api_app):
    """The flag plane is global (no target/desk column on `review_flags` yet),
    so the route reads the WHOLE table — every test starts from empty.

    `review_flags` has a schema-enforced never-delete trigger (0107); the sweep
    disables it for the duration of the truncate, which is the only sanctioned
    way to reset the table and is deliberately confined to tests.
    """
    _, pg_store = api_app

    async def _wipe() -> None:
        async with pg_store.acquire() as conn:
            await conn.execute(
                "ALTER TABLE review_flags DISABLE TRIGGER "
                "trg_review_flags_forbid_delete"
            )
            try:
                await conn.execute("DELETE FROM review_flags")
            finally:
                await conn.execute(
                    "ALTER TABLE review_flags ENABLE TRIGGER "
                    "trg_review_flags_forbid_delete"
                )
            await conn.execute(
                "DELETE FROM analyst_traces WHERE analyst_id = $1",
                cw.SUB_HANDLER_NAME,
            )

    await _wipe()
    yield
    await _wipe()


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


async def _consumer(conn, *, superseded: bool = False) -> UUID:
    cid = uuid4()
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, kind, title, body, confidence, data, analyst_id, schema_uri,
             superseded_by, superseded_at)
        VALUES ($1, 'finding', $2, '', 0.9, '{}'::jsonb, 'test_analyst',
                'iglu:legba/finding/jsonschema/1-0-0', $3,
                CASE WHEN $3::uuid IS NULL THEN NULL ELSE now() END)
        """,
        cid, f"consumer {cid}", uuid4() if superseded else None,
    )
    return cid


async def _flag(
    conn,
    output_id: UUID,
    *,
    founded_on_id: UUID | None = None,
    reason: str = "new_evidence_bears_on_open_question",
    closed: bool = False,
    created_at: datetime | None = None,
) -> UUID:
    fid = uuid4()
    await conn.execute(
        """
        INSERT INTO review_flags
            (id, output_id, founded_on_id, moved_at, reason, created_at,
             closed_by, closed_at)
        VALUES ($1, $2, $3, now(), $4, COALESCE($5, now()), $6,
                CASE WHEN $6::uuid IS NULL THEN NULL ELSE now() END)
        """,
        fid, output_id, founded_on_id or uuid4(), reason, created_at,
        uuid4() if closed else None,
    )
    return fid


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_plane_reads_honest_zero(client, clean_flags):
    r = await client.get(_ROUTE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["staleness_debt"] == 0
    assert body["open_flags"] == 0
    assert body["closed_flags"] == 0
    assert body["by_reason"] == []
    assert body["last_matcher_run_at"] is None
    assert body["match_verified"] is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_debt_counts_live_consumers_and_excludes_the_rest(
    api_app, client, clean_flags,
):
    """The three exclusions, end to end through HTTP: a live consumer counts, a
    SUPERSEDED consumer's still-open flag does not, and a CLOSED flag does
    not."""
    _, pg_store = api_app
    async with pg_store.acquire() as conn:
        live_a = await _consumer(conn)
        live_b = await _consumer(conn)
        gone = await _consumer(conn, superseded=True)
        closed_owner = await _consumer(conn)
        moved = uuid4()
        await _flag(conn, live_a, founded_on_id=moved)
        await _flag(conn, live_b, founded_on_id=moved)
        await _flag(conn, gone)                      # open, but consumer gone
        await _flag(conn, closed_owner, closed=True)  # closed by supersession

    r = await client.get(_ROUTE)
    body = r.json()
    assert body["staleness_debt"] == 2
    assert body["open_flags"] == 3
    assert body["superseded_consumer_flags"] == 1
    assert body["flagged_consumers"] == 2
    # Both live flags trace to the SAME moved foundation — one, not two.
    assert body["moved_foundations"] == 1
    assert body["closed_flags"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_headline_number_equals_the_matchers_own_query(
    api_app, client, clean_flags,
):
    """Belt and braces on top of the SQL drift guard: the number on the wire is
    what the matcher's constant returns against the same substrate."""
    _, pg_store = api_app
    async with pg_store.acquire() as conn:
        for _ in range(3):
            await _flag(conn, await _consumer(conn))
        await _flag(conn, await _consumer(conn, superseded=True))
        matcher_number = (await conn.fetchrow(cw._STALENESS_DEBT_SQL))["debt"]

    r = await client.get(_ROUTE)
    assert r.json()["staleness_debt"] == matcher_number == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reason_breakdown_and_open_window(api_app, client, clean_flags):
    _, pg_store = api_app
    old = datetime.now(tz=timezone.utc) - timedelta(days=5)
    async with pg_store.acquire() as conn:
        await _flag(conn, await _consumer(conn), created_at=old)
        await _flag(conn, await _consumer(conn))
        await _flag(
            conn, await _consumer(conn), reason="foundation_superseded",
        )

    body = (await client.get(_ROUTE)).json()
    assert body["by_reason"] == [
        {"reason": "new_evidence_bears_on_open_question", "open_flags": 2},
        {"reason": "foundation_superseded", "open_flags": 1},
    ]
    assert body["oldest_open_at"] is not None
    assert body["newest_open_at"] > body["oldest_open_at"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_last_matcher_run_distinguishes_zero_from_never_ran(
    api_app, client, clean_flags,
):
    """A genuine zero and a matcher that never ran are DIFFERENT states; the
    route must let a reader tell them apart."""
    _, pg_store = api_app
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_traces
                (run_id, analyst_id, analyst_version, cadence_trigger,
                 status, run_started_at, receipt_hash)
            VALUES ($1, $2, '1.0.0', 'test', 'ok', now(), 'test-receipt')
            """,
            uuid4(), cw.SUB_HANDLER_NAME,
        )
    body = (await client.get(_ROUTE)).json()
    assert body["staleness_debt"] == 0
    assert body["last_matcher_run_at"] is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_route_degrades_to_zeros_when_the_flag_plane_is_unreadable(
    api_app, client, clean_flags, monkeypatch,
):
    """Migration 0107 unapplied (or any query failure) must render "no data",
    not a 500 the System Status panel polls every few seconds."""
    _, pg_store = api_app
    async with pg_store.acquire() as conn:
        await _flag(conn, await _consumer(conn))

    broken = "SELECT count(*)::int AS debt FROM review_flags_does_not_exist"
    monkeypatch.setattr(v3_api, "_STALENESS_DEBT_SQL", broken)
    r = await client.get(_ROUTE)
    assert r.status_code == 200
    assert r.json() == {
        "staleness_debt": 0,
        "open_flags": 0,
        "superseded_consumer_flags": 0,
        "flagged_consumers": 0,
        "moved_foundations": 0,
        "closed_flags": 0,
        "oldest_open_at": None,
        "newest_open_at": None,
        "by_reason": [],
        "last_matcher_run_at": None,
        "match_verified": False,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_match_verified_is_hard_false_even_with_debt(
    api_app, client, clean_flags,
):
    """SEAMS #49 survives exposure: the closer is not built, so no payload may
    ever present this as a verified/closed number."""
    _, pg_store = api_app
    async with pg_store.acquire() as conn:
        await _flag(conn, await _consumer(conn))
    body = (await client.get(_ROUTE)).json()
    assert body["staleness_debt"] == 1
    assert body["match_verified"] is False
