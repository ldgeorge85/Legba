# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the P4-4 validity-window timeline route.

Covers :mod:`legba.data.registry.timeline_api`:

  * ``GET /api/v1/v3/timeline?target_id=&days=``  -> ``TimelineResponse``

Two layers, per the house v3-route pattern (the ``test_v3_since_api`` shape):

  * PURE tests (no DB): route registration + no-collision with the v3 / since
    routers, the registry-slim import guard, and the pure helpers
    (``fact_label`` / ``merge_items`` / the ``days`` bound).
  * INTEGRATION tests over the ephemeral ``migrated_pg`` database + real HTTP:
    ranged windows (open vs closed ``end``), the supersession-chain edge, the
    per-kind window filter + target filter, the honest empty state, the ``days``
    validation 400, and honest per-kind truncation counts.

Auth: tests run in dev-mode (``LEGBA_DEV_MODE=1`` from tests/conftest.py, no
``LEGBA_REGISTRY_API_TOKEN``), so ``require_bearer`` returns ``"anonymous"`` and
unauthenticated requests pass — the same bearer path as the rest of the v3
surface.
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

import legba.data.registry.timeline_api as timeline_api
from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.timeline_api import (
    MAX_DAYS,
    TimelineItem,
    build_timeline_router,
    fact_label,
    merge_items,
)
from legba.data.registry.vocabulary_cache import VocabularyCache

# Mandatory env for vault + signing identity (mirrors test_v3_since_api).
_TEST_MASTER_KEY_HEX = (
    "0011223344556677889900112233445566778899001122334455667788990011"
)
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "33" * 32)


def _fixed_identity() -> SigningIdentity:
    seed = b"v3-timeline-api-test-signing-see"
    assert len(seed) == 32
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:v3-timeline-test",
    )


# ---------------------------------------------------------------------------
# Pure tests — registration, slimness, helpers (no DB)
# ---------------------------------------------------------------------------


def test_timeline_route_registered() -> None:
    """The route registers and doesn't shadow an existing v3 / since path."""
    router = build_timeline_router(deps=object())  # type: ignore[arg-type]
    paths = {r.path for r in router.routes}  # type: ignore[attr-defined]
    assert "/timeline" in paths

    from legba.data.registry.since_api import build_since_router
    from legba.data.registry.v3_api import build_v3_router

    other = {
        r.path for r in build_v3_router(deps=object()).routes  # type: ignore[arg-type]
    } | {
        r.path for r in build_since_router(deps=object()).routes  # type: ignore[arg-type]
    }
    assert not (paths & other)


def test_timeline_registry_slim_no_runtime_imports() -> None:
    """The module stays registry-slim: no runtime / deterministic-handler
    imports (the v3_api / since_api slim-image rule)."""
    with open(timeline_api.__file__, "r", encoding="utf-8") as fh:
        text = fh.read()
    import_lines = "\n".join(
        ln for ln in text.splitlines()
        if ln.strip().startswith(("import ", "from "))
    )
    assert "deterministic" not in import_lines
    assert "legba.runtime" not in import_lines and "..runtime" not in import_lines


def test_fact_label_drops_empty_parts() -> None:
    assert fact_label("Iran", "allied_with", "Russia") == "Iran · allied_with · Russia"
    # Sparse triple never reads as a bare separator run.
    assert fact_label("Iran", "", None) == "Iran"
    assert fact_label(None, None, None) == "(fact)"


def test_merge_items_orders_newest_start_first() -> None:
    now = datetime.now(timezone.utc)

    def _it(kind: str, id_: str, start: datetime) -> TimelineItem:
        return TimelineItem(id=id_, kind=kind, label=id_, start=start, end=None)

    a = _it("fact", "a", now - timedelta(days=3))
    b = _it("situation", "b", now - timedelta(days=1))
    c = _it("finding", "c", now - timedelta(days=2))
    merged = merge_items([a], [b], [c])
    assert [it.id for it in merged] == ["b", "c", "a"]
    # Deterministic tie-break on id when starts are equal.
    t = now - timedelta(hours=1)
    x = _it("fact", "x", t)
    z = _it("fact", "z", t)
    assert [it.id for it in merge_items([x], [z])] == ["z", "x"]


def test_max_days_bound_is_ninety() -> None:
    assert MAX_DAYS == 90


# ---------------------------------------------------------------------------
# App fixture (ephemeral migrated DB + real HTTP — the since-route shape)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def timeline_app(migrated_pg: PostgresConfig):
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
    app.include_router(build_timeline_router(deps), prefix="/api/v1/v3")

    yield app, deps, pg_store

    await descriptor_registry.stop()
    await nats_store.close()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(timeline_app):
    app, _, _ = timeline_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Insertion helpers — direct SQL against the three temporal tables.
# ---------------------------------------------------------------------------


async def _insert_fact(
    pg_store: PostgresStore,
    *,
    subject: str,
    predicate: str = "is",
    value: str = "v",
    target_id: str | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    superseded_by: UUID | None = None,
    produced_at: datetime | None = None,
) -> UUID:
    row_id = uuid4()
    ts = produced_at or datetime.now(timezone.utc)
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO facts (
                id, subject, predicate, value, confidence, source_type,
                data, valid_from, valid_until, target_id, produced_at,
                derived_from, schema_uri, superseded_by
            ) VALUES (
                $1, $2, $3, $4, 1.0, 'agent',
                '{}'::jsonb, $5, $6, $7, $8,
                '{}', 'iglu:legba/fact/jsonschema/2-0-0', $9
            )
            """,
            row_id, subject, predicate, value, valid_from, valid_until,
            target_id, ts, superseded_by,
        )
    return row_id


async def _insert_situation(
    pg_store: PostgresStore,
    *,
    name: str,
    status_val: str = "active",
    target_id: str | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    last_event_at: datetime | None = None,
    produced_at: datetime | None = None,
) -> UUID:
    row_id = uuid4()
    ts = produced_at or datetime.now(timezone.utc)
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO situations (
                id, data, name, status, category, last_event_at,
                event_count, intensity_score, target_id, produced_at,
                derived_from, schema_uri, created_at, updated_at,
                valid_from, valid_until
            ) VALUES (
                $1, '{}'::jsonb, $2, $3, 'test', $4,
                1, 0.5, $5, $6,
                '{}', 'iglu:legba/situation/jsonschema/2-0-0', $6, $6,
                $7, $8
            )
            """,
            row_id, name, status_val, last_event_at, target_id, ts,
            valid_from, valid_until,
        )
    return row_id


async def _insert_finding(
    pg_store: PostgresStore,
    *,
    title: str,
    severity: str | None = "medium",
    target_id: str | None = None,
    produced_at: datetime | None = None,
    superseded_by: UUID | None = None,
    superseded_at: datetime | None = None,
) -> UUID:
    row_id = uuid4()
    ts = produced_at or datetime.now(timezone.utc)
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                target_id, analyst_id, produced_at, derived_from, schema_uri,
                superseded_by, superseded_at
            ) VALUES (
                $1, 'finding', $2, '', 0.8, $3, '{}'::jsonb,
                $4, 'test_analyst', $5, '{}',
                'iglu:legba/finding/jsonschema/1-0-0', $6, $7
            )
            """,
            row_id, title, severity, target_id, ts, superseded_by, superseded_at,
        )
    return row_id


def _by_id(body: dict) -> dict[str, dict]:
    return {it["id"]: it for it in body["items"]}


# ---------------------------------------------------------------------------
# /timeline — validation + empty state
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_days_validation(client: AsyncClient):
    # 0 and >90 -> clear 400 naming the bound.
    r = await client.get("/api/v1/v3/timeline", params={"days": 0})
    assert r.status_code == 400
    assert "days" in r.json()["detail"]
    r = await client.get("/api/v1/v3/timeline", params={"days": 91})
    assert r.status_code == 400


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_window_is_valid_envelope(client: AsyncClient):
    """A window with nothing in it returns a 200 all-empty envelope, not 404."""
    r = await client.get("/api/v1/v3/timeline", params={"days": 7})
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["counts"] == {"fact": 0, "situation": 0, "finding": 0}
    assert body["truncated"] == {"fact": False, "situation": False, "finding": False}


# ---------------------------------------------------------------------------
# /timeline — ranged windows (open vs closed end) + supersession
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ranged_windows_open_and_closed(client: AsyncClient, timeline_app):
    _, _, pg = timeline_app
    now = datetime.now(timezone.utc)

    # Open fact: valid_from set, valid_until NULL -> end=None (live window).
    open_fact = await _insert_fact(
        pg, subject="Open", valid_from=now - timedelta(days=2),
    )
    # Closed fact: valid_until inside the window -> end carried verbatim.
    closed_until = now - timedelta(days=1)
    closed_fact = await _insert_fact(
        pg, subject="Closed", valid_from=now - timedelta(days=5),
        valid_until=closed_until,
    )
    # Resolved situation: end = last_event_at (no valid_until).
    le = now - timedelta(hours=6)
    sit = await _insert_situation(
        pg, name="Resolved sit", status_val="resolved",
        produced_at=now - timedelta(days=3), last_event_at=le,
    )
    # Open finding (current head): superseded_at NULL -> end=None.
    finding = await _insert_finding(
        pg, title="Live finding", produced_at=now - timedelta(days=1),
    )

    r = await client.get("/api/v1/v3/timeline", params={"days": 30})
    assert r.status_code == 200
    body = r.json()
    items = _by_id(body)

    assert items[str(open_fact)]["end"] is None
    assert items[str(open_fact)]["kind"] == "fact"
    assert items[str(closed_fact)]["end"] is not None
    assert items[str(sit)]["end"] is not None  # resolved -> last_event_at
    assert items[str(sit)]["status"] == "resolved"
    assert items[str(finding)]["end"] is None   # live head
    assert items[str(finding)]["severity"] == "medium"

    # Newest window-start first: the finding (start -1d) leads the resolved
    # situation (start -3d) and the closed fact (start -5d).
    order = [it["id"] for it in body["items"]]
    assert order.index(str(finding)) < order.index(str(sit))
    assert order.index(str(sit)) < order.index(str(closed_fact))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_supersession_edge_is_surfaced(client: AsyncClient, timeline_app):
    _, _, pg = timeline_app
    now = datetime.now(timezone.utc)

    head = await _insert_finding(
        pg, title="Superseding head", produced_at=now - timedelta(hours=2),
    )
    superseded = await _insert_finding(
        pg, title="Old finding", produced_at=now - timedelta(days=2),
        superseded_by=head, superseded_at=now - timedelta(hours=2),
    )

    r = await client.get("/api/v1/v3/timeline", params={"days": 30})
    items = _by_id(r.json())
    # The superseded row points at its replacement (the chain edge) + closes.
    assert items[str(superseded)]["superseded_by"] == str(head)
    assert items[str(superseded)]["end"] is not None
    # The head is open (no supersession).
    assert items[str(head)]["superseded_by"] is None
    assert items[str(head)]["end"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_filter_scopes_items(client: AsyncClient, timeline_app):
    _, _, pg = timeline_app
    now = datetime.now(timezone.utc)
    mine = await _insert_finding(
        pg, title="mine", target_id="country_g20_ir",
        produced_at=now - timedelta(hours=1),
    )
    await _insert_finding(
        pg, title="theirs", target_id="country_g20_tr",
        produced_at=now - timedelta(hours=1),
    )

    r = await client.get(
        "/api/v1/v3/timeline", params={"target_id": "country_g20_ir", "days": 7},
    )
    body = r.json()
    ids = [it["id"] for it in body["items"]]
    assert ids == [str(mine)]
    assert body["target_id"] == "country_g20_ir"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_window_excludes_stale_rows(client: AsyncClient, timeline_app):
    _, _, pg = timeline_app
    now = datetime.now(timezone.utc)
    # Scope to a unique desk so the assertion is exact against the
    # session-shared migrated DB (rows from earlier tests accumulate).
    desk = "country_g20_stale_test"
    # A finding whose whole window sits before the 7d cutoff is excluded.
    ancient = await _insert_finding(
        pg, title="ancient", target_id=desk, produced_at=now - timedelta(days=40),
    )
    fresh = await _insert_finding(
        pg, title="fresh", target_id=desk, produced_at=now - timedelta(days=1),
    )
    r = await client.get(
        "/api/v1/v3/timeline", params={"target_id": desk, "days": 7},
    )
    ids = [it["id"] for it in r.json()["items"]]
    assert str(fresh) in ids
    assert str(ancient) not in ids
    assert ids == [str(fresh)]
