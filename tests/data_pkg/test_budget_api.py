# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the P-17 budget telemetry API surface.

Three GET endpoints under `/api/v1/budget/`:

  * `/ledger`    — budget_ledger rows w/ optional filters
  * `/envelope`  — global_budget_envelope state + live rollup
  * `/demotions` — budget_demotion_events audit list

These tests build a real FastAPI app against the real (migrated)
Postgres test DB — no mocks per Lewis's hard rule. We mirror the
fixture pattern from `test_registry_ui_panels_route.py` but stripped
down to the pieces the budget router needs (DescriptorRegistry for
the pg pool handle; nothing else is touched).
"""

from __future__ import annotations

# Resolve `legba` against this worktree's `src/` before any installed
# editable copy (the parent repo's pyproject installs `legba` from the
# checkout — when running tests on a host where a sibling worktree has
# the install, the import would otherwise pick up the older tree
# without `budget_api`). Harmless on a clean CI build where the worktree
# IS the install root.
#
# CRITICAL — only purge `legba.*` from sys.modules when the *already
# imported* `legba` actually resolves OUTSIDE this worktree's `src/`.
# A blanket purge corrupts module identity for the whole pytest process:
# sibling tests that captured class objects at top-of-module import
# (e.g. `AnalystMethodResult`, `LLMHandlerLike`) then fail their `is`
# identity assertions once a later `import` rebinds the freshly reloaded
# class. In the container/CI lane `legba` already loads from this src/
# tree (PYTHONPATH puts it first), so the purge must be a no-op there —
# the previous guard keyed off `sys.path[0]`, which is `''` under pytest
# and so wrongly fired the purge every run.
import sys as _sys
from pathlib import Path as _Path
_WT_SRC = str(_Path(__file__).resolve().parents[2] / "src")
_loaded = _sys.modules.get("legba")
_loaded_file = getattr(_loaded, "__file__", None) if _loaded is not None else None
_resolves_under_wt = bool(_loaded_file) and _Path(_loaded_file).resolve().is_relative_to(
    _Path(_WT_SRC).resolve()
)
if _loaded_file is None:
    # `legba` not imported yet — just make sure our src/ is first.
    if _WT_SRC not in _sys.path:
        _sys.path.insert(0, _WT_SRC)
elif not _resolves_under_wt:
    # A stale copy (sibling worktree install) is shadowing us — reinsert
    # our src/ and drop the stale modules so the next import rebinds.
    _sys.path.insert(0, _WT_SRC)
    for _name in list(_sys.modules):
        if _name == "legba" or _name.startswith("legba."):
            del _sys.modules[_name]

import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.budget_api import build_budget_router
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.emitter import NATSEventEmitter
from legba.data.registry.signing import load_default_identity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache


_TEST_MASTER_KEY_HEX = (
    "0011223344556677889900112233445566778899001122334455667788990011"
)
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "33" * 32)


# ---------------------------------------------------------------------------
# Fixtures — real app + client wired to the migrated test DB.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    """A FastAPI app exposing only the budget router. Returns
    (app, deps, pg_store) so tests can seed budget_ledger /
    global_budget_envelope / budget_demotion_events rows directly.
    """
    os.environ.pop(API_TOKEN_ENV, None)  # dev mode — any bearer accepted

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    nats_store = NatsStore(NatsConfig.from_env())
    await nats_store.connect()

    identity = load_default_identity()
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)
    emitter = NATSEventEmitter(nats_store)

    descriptor_registry = DescriptorRegistry(
        pg_store,
        nats_store=nats_store,
        vocabulary_cache=vocab,
        signing_identity=identity,
        audit_logger=audit,
        dead_letter=dlq,
    )
    stack_registry = StackRegistry(
        pg_store, vault, audit=audit, emitter=emitter, dlq=dlq,
    )

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
    app.include_router(build_budget_router(deps), prefix="/api/v1/budget")

    yield app, deps, pg_store

    await nats_store.close()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(api_app):
    app, _, _ = api_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Per-test isolation — each test runs against a freshly truncated set of
# budget tables. The migrated_pg fixture is session-scoped (shared) so
# tests must not contaminate each other's state.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _isolate_budget_tables(api_app):
    _, _, pg_store = api_app
    async with pg_store.acquire() as conn:
        await conn.execute("TRUNCATE budget_ledger")
        await conn.execute("TRUNCATE budget_demotion_events")
        await conn.execute("TRUNCATE global_budget_envelope")
    yield
    async with pg_store.acquire() as conn:
        await conn.execute("TRUNCATE budget_ledger")
        await conn.execute("TRUNCATE budget_demotion_events")
        await conn.execute("TRUNCATE global_budget_envelope")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_utc() -> date:
    return datetime.now(tz=timezone.utc).date()


async def _insert_ledger_row(
    pg_store: PostgresStore,
    *,
    analyst_id: str,
    analyst_version: str = "ff" * 8,
    bucket: date | None = None,
    tokens_used: int = 0,
    runs: int = 1,
    cost_estimate_usd: Decimal = Decimal("0"),
) -> None:
    bucket = bucket or _today_utc()
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO budget_ledger
                (analyst_id, analyst_version, bucket, tokens_used, runs,
                 cost_estimate_usd)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            analyst_id,
            analyst_version,
            bucket,
            tokens_used,
            runs,
            cost_estimate_usd,
        )


async def _insert_envelope(
    pg_store: PostgresStore,
    *,
    bucket: date | None = None,
    tokens_cap: int | None = None,
    usd_cap: Decimal | None = None,
    on_exceeded: str = "demote_all",
    note: str | None = None,
) -> None:
    bucket = bucket or _today_utc()
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO global_budget_envelope
                (bucket, tokens_cap, usd_cap, on_exceeded, note)
            VALUES ($1, $2, $3, $4, $5)
            """,
            bucket, tokens_cap, usd_cap, on_exceeded, note,
        )


async def _insert_demotion(
    pg_store: PostgresStore,
    *,
    analyst_id: str,
    analyst_version: str = "ff" * 8,
    bucket: date | None = None,
    cause: str = "per_analyst",
    tokens_used_at_demote: int | None = None,
    tokens_cap_at_demote: int | None = None,
    primary_llm: str | None = None,
    fallback_llm: str | None = None,
) -> None:
    bucket = bucket or _today_utc()
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO budget_demotion_events
                (analyst_id, analyst_version, bucket, cause,
                 tokens_used_at_demote, tokens_cap_at_demote,
                 primary_llm, fallback_llm)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            analyst_id, analyst_version, bucket, cause,
            tokens_used_at_demote, tokens_cap_at_demote,
            primary_llm, fallback_llm,
        )


# ===========================================================================
# /ledger
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ledger_empty_returns_empty_list(client):
    r = await client.get("/api/v1/budget/ledger")
    assert r.status_code == 200, r.text
    assert r.json() == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ledger_returns_row_after_insert(api_app, client):
    _, _, pg_store = api_app
    analyst = f"a_{uuid4().hex[:8]}"
    await _insert_ledger_row(
        pg_store,
        analyst_id=analyst,
        tokens_used=1234,
        runs=3,
        cost_estimate_usd=Decimal("0.012345"),
    )

    r = await client.get("/api/v1/budget/ledger")
    assert r.status_code == 200, r.text
    payload = r.json()
    rows = [p for p in payload if p["analyst_id"] == analyst]
    assert len(rows) == 1
    row = rows[0]
    assert row["tokens_used"] == 1234
    assert row["runs"] == 3
    # Decimal serialized as string to preserve NUMERIC(12,6) precision.
    assert row["cost_estimate_usd"] == "0.012345"
    assert isinstance(row["cost_estimate_usd"], str)
    assert row["cost_usd"] == "0.000000"  # default-zero stamped column
    assert row["bucket"] == _today_utc().isoformat()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ledger_filters_by_analyst_id(api_app, client):
    _, _, pg_store = api_app
    a1 = f"a_{uuid4().hex[:8]}"
    a2 = f"a_{uuid4().hex[:8]}"
    await _insert_ledger_row(pg_store, analyst_id=a1, tokens_used=100)
    await _insert_ledger_row(pg_store, analyst_id=a2, tokens_used=200)

    r = await client.get(
        "/api/v1/budget/ledger", params={"analyst_id": a1},
    )
    assert r.status_code == 200
    payload = r.json()
    assert len(payload) == 1
    assert payload[0]["analyst_id"] == a1
    assert payload[0]["tokens_used"] == 100


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ledger_filters_by_date_range(api_app, client):
    _, _, pg_store = api_app
    analyst = f"a_{uuid4().hex[:8]}"
    today = _today_utc()
    three_days_ago = today - timedelta(days=3)
    one_week_ago = today - timedelta(days=7)

    await _insert_ledger_row(
        pg_store, analyst_id=analyst, bucket=one_week_ago, tokens_used=10,
    )
    await _insert_ledger_row(
        pg_store, analyst_id=analyst, bucket=three_days_ago, tokens_used=20,
    )
    await _insert_ledger_row(
        pg_store, analyst_id=analyst, bucket=today, tokens_used=30,
    )

    # `from` covers only the recent two.
    r = await client.get(
        "/api/v1/budget/ledger",
        params={
            "analyst_id": analyst,
            "from": (today - timedelta(days=5)).isoformat(),
        },
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    buckets = {row["bucket"] for row in rows}
    assert buckets == {three_days_ago.isoformat(), today.isoformat()}

    # `to` covers only the older two.
    r = await client.get(
        "/api/v1/budget/ledger",
        params={
            "analyst_id": analyst,
            "to": (today - timedelta(days=1)).isoformat(),
        },
    )
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    assert {row["bucket"] for row in rows} == {
        one_week_ago.isoformat(),
        three_days_ago.isoformat(),
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ledger_bucket_granularity_hour_documents_limitation(
    api_app, client,
):
    """Per the docstring: 'hour' is accepted for forward compat but
    returns the same day-grained rows as 'day' (no synthetic hour
    buckets manufactured from day rows)."""
    _, _, pg_store = api_app
    analyst = f"a_{uuid4().hex[:8]}"
    await _insert_ledger_row(pg_store, analyst_id=analyst, tokens_used=500)

    r_day = await client.get(
        "/api/v1/budget/ledger",
        params={"analyst_id": analyst, "bucket_granularity": "day"},
    )
    r_hour = await client.get(
        "/api/v1/budget/ledger",
        params={"analyst_id": analyst, "bucket_granularity": "hour"},
    )
    assert r_day.status_code == 200
    assert r_hour.status_code == 200
    assert r_day.json() == r_hour.json()


# ===========================================================================
# /envelope
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_envelope_no_row_returns_null_caps_zero_rollup(client):
    """No envelope row + no ledger rows for the bucket → all caps NULL,
    rollup zero, demoted NULL (no cap → no demotion possible)."""
    r = await client.get("/api/v1/budget/envelope")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tokens_cap"] is None
    assert body["usd_cap"] is None
    assert body["on_exceeded"] is None
    assert body["note"] is None
    assert body["current_tokens"] == 0
    assert body["current_cost_usd"] == "0"
    assert body["demoted"] is None
    assert body["last_updated"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_envelope_row_present_returns_full_state(api_app, client):
    """Envelope row present + ledger rows → caps surfaced, rollup
    populated, demoted reflects whether either dimension trips."""
    _, _, pg_store = api_app
    await _insert_envelope(
        pg_store,
        tokens_cap=10_000,
        usd_cap=Decimal("5.00"),
        on_exceeded="demote_all",
        note="test envelope",
    )
    a1 = f"a_{uuid4().hex[:8]}"
    a2 = f"a_{uuid4().hex[:8]}"
    await _insert_ledger_row(
        pg_store, analyst_id=a1, tokens_used=2000,
        cost_estimate_usd=Decimal("0.50"),
    )
    await _insert_ledger_row(
        pg_store, analyst_id=a2, tokens_used=3000,
        cost_estimate_usd=Decimal("1.25"),
    )

    r = await client.get("/api/v1/budget/envelope")
    assert r.status_code == 200
    body = r.json()
    assert body["tokens_cap"] == 10_000
    assert body["usd_cap"] == "5.000000"  # NUMERIC(12,6) stringified
    assert body["on_exceeded"] == "demote_all"
    assert body["note"] == "test envelope"
    assert body["current_tokens"] == 5000
    assert body["current_cost_usd"] == "1.750000"
    assert body["demoted"] is False  # 5000 < 10000 and 1.75 < 5.00
    assert body["last_updated"] is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_envelope_demoted_true_when_tokens_exhausted(api_app, client):
    _, _, pg_store = api_app
    await _insert_envelope(pg_store, tokens_cap=1000)
    await _insert_ledger_row(
        pg_store, analyst_id="exhauster", tokens_used=1500,
    )

    r = await client.get("/api/v1/budget/envelope")
    assert r.status_code == 200
    body = r.json()
    assert body["current_tokens"] == 1500
    assert body["demoted"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_envelope_demoted_true_when_usd_exhausted(api_app, client):
    _, _, pg_store = api_app
    await _insert_envelope(pg_store, usd_cap=Decimal("1.000000"))
    await _insert_ledger_row(
        pg_store, analyst_id="dollar_hog",
        cost_estimate_usd=Decimal("2.000000"), tokens_used=0,
    )

    r = await client.get("/api/v1/budget/envelope")
    body = r.json()
    assert body["demoted"] is True
    assert body["tokens_cap"] is None
    assert body["usd_cap"] == "1.000000"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_envelope_for_explicit_bucket(api_app, client):
    """`?bucket=YYYY-MM-DD` selects a specific day."""
    _, _, pg_store = api_app
    past = _today_utc() - timedelta(days=5)
    await _insert_envelope(pg_store, bucket=past, tokens_cap=500)
    await _insert_ledger_row(
        pg_store, analyst_id="x", bucket=past, tokens_used=100,
    )

    r = await client.get(
        "/api/v1/budget/envelope", params={"bucket": past.isoformat()},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["bucket"] == past.isoformat()
    assert body["tokens_cap"] == 500
    assert body["current_tokens"] == 100


# ===========================================================================
# /demotions
# ===========================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_demotions_empty(client):
    r = await client.get("/api/v1/budget/demotions")
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_demotions_lists_and_filters_by_analyst(api_app, client):
    _, _, pg_store = api_app
    a1 = f"a_{uuid4().hex[:8]}"
    a2 = f"a_{uuid4().hex[:8]}"
    await _insert_demotion(
        pg_store, analyst_id=a1, cause="per_analyst",
        tokens_used_at_demote=2000, tokens_cap_at_demote=1000,
        primary_llm="anthropic:claude-opus-4-7",
        fallback_llm="vllm:gpt-oss-120b",
    )
    await _insert_demotion(
        pg_store, analyst_id=a2, cause="global",
        tokens_used_at_demote=10_500, tokens_cap_at_demote=10_000,
    )

    # No filter → both.
    r = await client.get("/api/v1/budget/demotions")
    assert r.status_code == 200
    payload = r.json()
    assert len(payload) >= 2
    analysts = {e["analyst_id"] for e in payload}
    assert {a1, a2}.issubset(analysts)

    # Filter to one.
    r_a1 = await client.get(
        "/api/v1/budget/demotions", params={"analyst_id": a1},
    )
    assert r_a1.status_code == 200
    rows = r_a1.json()
    assert len(rows) == 1
    assert rows[0]["analyst_id"] == a1
    assert rows[0]["cause"] == "per_analyst"
    assert rows[0]["tokens_used_at_demote"] == 2000
    assert rows[0]["tokens_cap_at_demote"] == 1000
    assert rows[0]["primary_llm"] == "anthropic:claude-opus-4-7"
    assert rows[0]["fallback_llm"] == "vllm:gpt-oss-120b"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_demotions_limit_paginates(api_app, client):
    _, _, pg_store = api_app
    analyst = f"a_{uuid4().hex[:8]}"
    for _ in range(5):
        await _insert_demotion(pg_store, analyst_id=analyst)

    r = await client.get(
        "/api/v1/budget/demotions",
        params={"analyst_id": analyst, "limit": 2},
    )
    assert r.status_code == 200
    assert len(r.json()) == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_demotions_since_filters(api_app, client):
    _, _, pg_store = api_app
    analyst = f"a_{uuid4().hex[:8]}"
    await _insert_demotion(pg_store, analyst_id=analyst)
    # The row's occurred_at is NOW(); since=NOW()+1h returns nothing.
    future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    r = await client.get(
        "/api/v1/budget/demotions",
        params={"analyst_id": analyst, "since": future.isoformat()},
    )
    assert r.status_code == 200
    assert r.json() == []
