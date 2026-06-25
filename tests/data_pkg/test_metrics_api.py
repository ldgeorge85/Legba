# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Prometheus ``/metrics`` exposition (resilience P2).

Two layers:

  * ``render_exposition`` — pure text-format rendering, unit-tested with no
    DB (label escaping, HELP/TYPE headers, empty-family headers, trailing
    newline).
  * ``GET /metrics`` — a real FastAPI app against the migrated test DB (no
    mocks per Lewis's hard rule): seed budget_ledger + an open DLQ row, scrape
    the endpoint, assert the live counters/gauges land in the exposition.

Mirrors the fixture pattern in ``test_budget_api.py`` — DescriptorRegistry
for the pg pool handle, nothing else touched.
"""

from __future__ import annotations

# Resolve `legba` against this worktree's `src/` first (see test_budget_api
# for the full rationale; this guard is a no-op in the container/CI lane).
import sys as _sys
from pathlib import Path as _Path
_WT_SRC = str(_Path(__file__).resolve().parents[2] / "src")
_loaded = _sys.modules.get("legba")
_loaded_file = getattr(_loaded, "__file__", None) if _loaded is not None else None
_resolves_under_wt = bool(_loaded_file) and _Path(_loaded_file).resolve().is_relative_to(
    _Path(_WT_SRC).resolve()
)
if _loaded_file is None:
    if _WT_SRC not in _sys.path:
        _sys.path.insert(0, _WT_SRC)
elif not _resolves_under_wt:
    _sys.path.insert(0, _WT_SRC)
    for _name in list(_sys.modules):
        if _name == "legba" or _name.startswith("legba."):
            del _sys.modules[_name]

import os
from datetime import date, datetime, timezone
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
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.emitter import NATSEventEmitter
from legba.data.registry.metrics_api import (
    build_metrics_router,
    render_exposition,
)
from legba.data.registry.signing import load_default_identity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache


_TEST_MASTER_KEY_HEX = (
    "0011223344556677889900112233445566778899001122334455667788990011"
)
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "33" * 32)


# ---------------------------------------------------------------------------
# render_exposition — pure unit tests (no DB)
# ---------------------------------------------------------------------------


def _parse_lines(text: str) -> list[str]:
    return [ln for ln in text.split("\n") if ln]


def test_render_basic_family():
    out = render_exposition(
        families=[
            {
                "name": "legba_signals_total",
                "type": "counter",
                "help": "Signals ingested.",
                "samples": [{"labels": {}, "value": 42}],
            }
        ]
    )
    lines = _parse_lines(out)
    assert "# HELP legba_signals_total Signals ingested." in lines
    assert "# TYPE legba_signals_total counter" in lines
    assert "legba_signals_total 42" in lines
    # Exposition format requires a trailing newline.
    assert out.endswith("\n")


def test_render_labels_and_escaping():
    out = render_exposition(
        families=[
            {
                "name": "legba_findings_total",
                "type": "counter",
                "help": "Outputs by kind.",
                "samples": [
                    {"labels": {"kind": 'fin"d\\ing'}, "value": 3},
                ],
            }
        ]
    )
    # The quote + backslash in the label value must be escaped.
    assert 'kind="fin\\"d\\\\ing"' in out
    assert "legba_findings_total{" in out


def test_render_empty_family_keeps_header():
    """A family with no samples still emits HELP/TYPE so scrapers see it."""
    out = render_exposition(
        families=[
            {
                "name": "legba_signal_ingest_age_seconds",
                "type": "gauge",
                "help": "Ingest age.",
                "samples": [],
            }
        ]
    )
    lines = _parse_lines(out)
    assert "# TYPE legba_signal_ingest_age_seconds gauge" in lines
    # No value line for the empty family.
    assert not any(
        ln.startswith("legba_signal_ingest_age_seconds ") for ln in lines
    )


def test_render_float_value():
    out = render_exposition(
        families=[
            {
                "name": "legba_analyst_cost_estimate_usd",
                "type": "gauge",
                "help": "Cost.",
                "samples": [{"labels": {"analyst_id": "a"}, "value": 1.5}],
            }
        ]
    )
    assert 'legba_analyst_cost_estimate_usd{analyst_id="a"} 1.5' in out


# ---------------------------------------------------------------------------
# Live /metrics endpoint — real app against the migrated test DB
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def metrics_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)  # dev mode

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
    app.include_router(build_metrics_router(deps))

    yield app, pg_store

    await nats_store.close()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(metrics_app):
    app, _ = metrics_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


@pytest_asyncio.fixture(autouse=True)
async def _isolate(metrics_app):
    _, pg_store = metrics_app
    async with pg_store.acquire() as conn:
        await conn.execute("TRUNCATE budget_ledger")
        await conn.execute("TRUNCATE global_budget_envelope")
        await conn.execute("TRUNCATE descriptor_dead_letter")
    yield
    async with pg_store.acquire() as conn:
        await conn.execute("TRUNCATE budget_ledger")
        await conn.execute("TRUNCATE global_budget_envelope")
        await conn.execute("TRUNCATE descriptor_dead_letter")


def _today() -> date:
    return datetime.now(tz=timezone.utc).date()


@pytest.mark.asyncio
async def test_metrics_endpoint_content_type_and_scrape_ok(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    # A clean scrape flips the health gauge to 1.
    assert "legba_metrics_scrape_ok 1" in body
    # Static families always present.
    assert "# TYPE legba_signals_total counter" in body
    assert "# TYPE legba_dlq_open gauge" in body


@pytest.mark.asyncio
async def test_metrics_reflects_open_dlq(client, metrics_app):
    _, pg_store = metrics_app
    async with pg_store.acquire() as conn:
        # migrated_pg is shared across the suite and other test files insert
        # dead-letter rows; clear both queues so the absolute count below is
        # deterministic regardless of test ordering.
        await conn.execute("DELETE FROM descriptor_dead_letter")
        await conn.execute("DELETE FROM output_dead_letter")
        await conn.execute(
            """
            INSERT INTO descriptor_dead_letter
                (id, actor, namespace, attempted_payload, validation_error)
            VALUES ($1, 'test-actor', 'source',
                    '{}'::jsonb, '{"err": "bad"}'::jsonb)
            """,
            uuid4(),
        )
    resp = await client.get("/metrics")
    body = resp.text
    assert 'legba_dlq_open{queue="descriptor"} 1' in body
    assert 'legba_dlq_open{queue="output"} 0' in body


@pytest.mark.asyncio
async def test_metrics_reflects_budget_ledger(client, metrics_app):
    _, pg_store = metrics_app
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO budget_ledger
                (analyst_id, analyst_version, bucket, tokens_used, runs,
                 cost_estimate_usd)
            VALUES ('country_assessor', $1, $2, 1234, 3, 0.5)
            """,
            "ff" * 8,
            _today(),
        )
    resp = await client.get("/metrics")
    body = resp.text
    assert (
        'legba_analyst_tokens_used{analyst_id="country_assessor"} 1234'
        in body
    )
    assert "legba_budget_envelope_tokens_used 1234" in body


@pytest.mark.asyncio
async def test_metrics_emits_envelope_cap_when_configured(client, metrics_app):
    _, pg_store = metrics_app
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO global_budget_envelope
                (bucket, tokens_cap, on_exceeded)
            VALUES ($1, 1000000, 'demote_all')
            """,
            _today(),
        )
    resp = await client.get("/metrics")
    body = resp.text
    assert "legba_budget_envelope_tokens_cap 1000000" in body


@pytest.mark.asyncio
async def test_metrics_omits_cap_when_unset(client):
    """No envelope row for today → the cap series is absent (not 0)."""
    resp = await client.get("/metrics")
    body = resp.text
    assert "legba_budget_envelope_tokens_cap" not in body
