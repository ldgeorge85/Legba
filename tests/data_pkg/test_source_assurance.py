# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P3-1 integration tests — source assurance ledger, layers 1+2 (A6).

Covers, against the live substrate via the ``migrated_pg`` fixture (fresh
``legba_test_<uuid>`` DB migrated through 0094 — no mocks for substrate
boundaries):

  * rating upsert + supersession history (``source_ratings`` chain);
  * multi-rater concurrency — a public catalog rating and a private annex
    rating for the SAME source coexist as current rows;
  * the ``/api/v1/v3/sources/{id}/assurance`` route's visibility filtering
    (private rows only on explicit ``include_private`` opt-in);
  * the example-YAML loader round-trip (idempotent re-run) against the
    tracked schema doc ``seeds/source_ratings.example.yaml``;
  * the additive ``assurance_grade`` projection on the P-05 ``/sources``
    list + detail read views.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.api import (
    API_TOKEN_ENV,
    RegistryAPIDeps,
    build_router,
)
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.source_assurance_api import (
    build_source_assurance_router,
)
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache
from legba.data.seed.source_ratings_loader import (
    RatingSpec,
    seed_source_ratings,
    upsert_rating,
)

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "33" * 32)

EXAMPLE_YAML = Path(__file__).resolve().parents[2] / "seeds" / "source_ratings.example.yaml"


def _fixed_identity() -> SigningIdentity:
    seed = b"p3-1-source-assurance-test-seedXX"[:32]
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:p3-1-assurance-test",
    )


def _source_body(descriptor_id: str) -> dict[str, Any]:
    """Minimal P-05 source descriptor body (the registry REST register shape)."""
    return {
        "identity": {
            "id": descriptor_id,
            "name": "Assurance Test Wire",
            "kind": "rss",
            "schema_uri": "legba/source/1.0.0",
            "version": "0" * 16,
            "abstraction_level": "L1",
            "state": "draft",
            "owner": "lewis@local",
            "created": "2026-07-24T10:00:00+00:00",
        },
        "scope": {
            "owner_tenant": "default",
            "geo": ["US"],
            "languages": ["en"],
            "tags": ["news"],
        },
        "acquisition": "poll",
        "cadence": {"schedule": {"raw": "*/15 * * * *"}},
        "subscription_policy": "open",
        "output": {"delivery": "lossy"},
    }


def _spec(
    source_id: str,
    *,
    rater: str = "catalog:test-catalog",
    reliability: str | None = "B",
    credibility: str | None = "2",
    rubric: dict[str, Any] | None = None,
    references: list[dict[str, Any]] | None = None,
) -> RatingSpec:
    return RatingSpec(
        source_id=source_id,
        rater=rater,
        admiralty_reliability=reliability,
        admiralty_credibility=credibility,
        rubric=rubric or {"type": "news_agency", "state_affiliation": "none"},
        references=references
        or [{"url": "https://example.org/x", "title": "Example ref"}],
        rated_at=None,
    )


# ---------------------------------------------------------------------------
# Wiring fixture (no NATS — descriptor events are observability-only)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    identity = _fixed_identity()
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
    stack_registry = StackRegistry(pg_store, vault, audit=audit, dlq=dlq)

    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry,
        stack_registry=stack_registry,
        vault=vault,
        dlq=dlq,
        audit_logger=audit,
        vocabulary_cache=vocab,
        nats_store=None,
    )

    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_router(deps), prefix="/api/v1/registry")
    app.include_router(build_source_assurance_router(deps), prefix="/api/v1/v3")

    yield app, deps, pg_store

    await descriptor_registry.stop()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(api_app):
    app, _, _ = api_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Layer 2 core — upsert + supersession history
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rating_upsert_supersession_history(api_app):
    _, _, pg_store = api_app
    source_id = f"source.rss.assurance_{uuid4().hex[:8]}"

    async with pg_store.acquire() as conn:
        # First write → inserted.
        assert await upsert_rating(conn, _spec(source_id)) == "inserted"
        # Content-identical re-run → idempotent no-op.
        assert await upsert_rating(conn, _spec(source_id)) == "unchanged"
        # Changed grade → history: new current row, old row chained behind it.
        assert (
            await upsert_rating(
                conn, _spec(source_id, reliability="C", credibility="3"),
            )
            == "superseded"
        )

        rows = await conn.fetch(
            "SELECT id, admiralty_reliability, admiralty_credibility, "
            "       superseded_by, method, visibility_class "
            "  FROM source_ratings WHERE source_id = $1 ORDER BY rated_at",
            source_id,
        )
    assert len(rows) == 2
    current = [r for r in rows if r["superseded_by"] is None]
    assert len(current) == 1
    assert current[0]["admiralty_reliability"] == "C"
    assert current[0]["admiralty_credibility"] == "3"
    assert current[0]["method"] == "catalog_seed"
    assert current[0]["visibility_class"] == "public"
    # The superseded row points at the row that replaced it.
    old = [r for r in rows if r["superseded_by"] is not None]
    assert old[0]["superseded_by"] == current[0]["id"]
    assert old[0]["admiralty_reliability"] == "B"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_multi_rater_public_and_private_coexist(api_app):
    """Day-one multi-rater: a public catalog rating and a private annex rating
    for the SAME source are DISTINCT current rows (no supersession across
    raters / visibility classes)."""
    _, _, pg_store = api_app
    source_id = f"source.rss.assurance_{uuid4().hex[:8]}"

    async with pg_store.acquire() as conn:
        assert (
            await upsert_rating(
                conn, _spec(source_id, rater="catalog:public-cat"),
            )
            == "inserted"
        )
        assert (
            await upsert_rating(
                conn,
                _spec(
                    source_id,
                    rater="annex:acme-corp",
                    reliability="E",
                    credibility="5",
                ),
                method="operator",
                visibility_class="private",
            )
            == "inserted"
        )
        current = await conn.fetch(
            "SELECT rater, visibility_class, method FROM source_ratings "
            " WHERE source_id = $1 AND superseded_by IS NULL",
            source_id,
        )
    assert len(current) == 2
    by_rater = {r["rater"]: r for r in current}
    assert by_rater["catalog:public-cat"]["visibility_class"] == "public"
    assert by_rater["annex:acme-corp"]["visibility_class"] == "private"
    assert by_rater["annex:acme-corp"]["method"] == "operator"


# ---------------------------------------------------------------------------
# The assurance route — visibility filtering + dossier
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_assurance_route_visibility_filtering(api_app, client):
    _, _, pg_store = api_app
    source_id = f"source.rss.assurance_{uuid4().hex[:8]}"

    async with pg_store.acquire() as conn:
        await upsert_rating(conn, _spec(source_id, rater="catalog:public-cat"))
        await upsert_rating(
            conn,
            _spec(source_id, rater="annex:acme", reliability="E", credibility="5"),
            method="operator",
            visibility_class="private",
        )
        # A current dossier (layer 1) beside the ratings.
        await conn.execute(
            """
            INSERT INTO source_dossiers
              (source_id, dossier_md, refs, compiled_by)
            VALUES ($1, $2, $3::jsonb, $4)
            """,
            source_id,
            "## Ownership\nOwned by Example Wire Holdings [1].",
            '[{"url": "https://example.org/filing", "title": "Ownership filing"}]',
            "operator:test",
        )

    # Default: PUBLIC rows only.
    r = await client.get(f"/api/v1/v3/sources/{source_id}/assurance")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_id"] == source_id
    assert body["registered"] is False  # ratings may precede registration
    assert body["includes_private"] is False
    assert [x["rater"] for x in body["ratings"]] == ["catalog:public-cat"]
    assert body["ratings"][0]["grade"] == "B2"
    assert body["ratings"][0]["references"][0]["url"] == "https://example.org/x"
    assert body["assurance_grade"] == "B2"
    assert body["dossier"]["compiled_by"] == "operator:test"
    assert "Example Wire Holdings" in body["dossier"]["dossier_md"]
    assert body["dossier"]["references"][0]["title"] == "Ownership filing"

    # Opt-in: private annex rows appear; the PUBLIC grade is unchanged
    # (private never drives the public assurance_grade).
    r = await client.get(
        f"/api/v1/v3/sources/{source_id}/assurance",
        params={"include_private": "1"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["includes_private"] is True
    raters = {x["rater"]: x for x in body["ratings"]}
    assert set(raters) == {"catalog:public-cat", "annex:acme"}
    assert raters["annex:acme"]["visibility_class"] == "private"
    assert raters["annex:acme"]["grade"] == "E5"
    assert body["assurance_grade"] == "B2"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_assurance_route_unknown_source_404(client):
    r = await client.get(
        f"/api/v1/v3/sources/source.nope.{uuid4().hex[:8]}/assurance",
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Example-YAML loader round-trip (against the tracked schema doc)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_example_yaml_loader_round_trip(api_app):
    assert EXAMPLE_YAML.exists(), f"schema doc missing: {EXAMPLE_YAML}"
    _, _, pg_store = api_app

    async with pg_store.acquire() as conn:
        first = await seed_source_ratings(conn, EXAMPLE_YAML)
        assert first.errors == []
        assert first.inserted == 3
        assert first.superseded == 0

        # Idempotent re-run: content-identical rows are no-ops.
        second = await seed_source_ratings(conn, EXAMPLE_YAML)
        assert second.errors == []
        assert second.inserted == 0
        assert second.superseded == 0
        assert second.unchanged == 3

        rows = await conn.fetch(
            """
            SELECT source_id, rater, method, visibility_class,
                   admiralty_reliability || admiralty_credibility AS grade,
                   rubric
              FROM source_ratings
             WHERE rater = 'catalog:example-catalog'
               AND superseded_by IS NULL
             ORDER BY source_id
            """,
        )
    grades = {r["source_id"]: r["grade"] for r in rows}
    assert grades == {
        "source.rss.examplewire_world": "B2",
        "source.rss.tribune_national_daily": "C3",
        "source.telegram.frontline_watch_channel": "D4",
    }
    for r in rows:
        assert r["method"] == "catalog_seed"
        assert r["visibility_class"] == "public"
    rubric = {r["source_id"]: r["rubric"] for r in rows}[
        "source.rss.tribune_national_daily"
    ]
    assert rubric["state_affiliation"] == "state_aligned"
    assert rubric["type"] == "newspaper"


# ---------------------------------------------------------------------------
# P-05 projection — additive assurance_grade on /sources
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_source_list_projection_assurance_grade(api_app, client):
    _, _, pg_store = api_app
    desc_id = f"source.rss.assurance_{uuid4().hex[:8]}"
    # RETIRE the registered descriptor on the way out, failure path included.
    # This registration was the polluter behind the 2026-08-17/18 nightly
    # shuffled FAILs: the head stayed in the session-shared source_descriptors
    # table, and test_collection_requirements' candidate assertions are
    # statements about that whole table, so its clean_slate sentinel
    # (correctly) refused to run them. Scoped to exactly the rows this test
    # created, per the m23 idiom — a suite-wide wipe here would make THIS
    # file the polluter.
    try:

        # Register a real source descriptor through the REST lifecycle.
        r = await client.post(
            "/api/v1/registry/descriptors/source", json=_source_body(desc_id),
        )
        assert r.status_code == 201, r.text

        def _row(payload: list[dict[str, Any]]) -> dict[str, Any]:
            matches = [x for x in payload if x["descriptor_id"] == desc_id]
            assert len(matches) == 1
            return matches[0]

        # Ungraded: the additive field is present and null (shape stays additive —
        # every pre-P3-1 field is untouched).
        r = await client.get("/api/v1/registry/sources")
        assert r.status_code == 200, r.text
        row = _row(r.json())
        assert row["assurance_grade"] is None
        assert row["kind"] == "rss"  # pre-existing projection intact

        # A PRIVATE rating must NOT surface in the public projection.
        async with pg_store.acquire() as conn:
            await upsert_rating(
                conn,
                _spec(desc_id, rater="annex:acme", reliability="A", credibility="1"),
                method="operator",
                visibility_class="private",
            )
        r = await client.get("/api/v1/registry/sources")
        assert _row(r.json())["assurance_grade"] is None

        # A public catalog rating surfaces as the display grade.
        async with pg_store.acquire() as conn:
            await upsert_rating(conn, _spec(desc_id))
        r = await client.get("/api/v1/registry/sources")
        assert _row(r.json())["assurance_grade"] == "B2"

        # Detail view carries the same stamp; route now reports registered=true.
        r = await client.get(f"/api/v1/registry/sources/{desc_id}")
        assert r.status_code == 200, r.text
        assert r.json()["assurance_grade"] == "B2"

        r = await client.get(f"/api/v1/v3/sources/{desc_id}/assurance")
        assert r.status_code == 200, r.text
        assert r.json()["registered"] is True
    finally:
        async with pg_store.acquire() as conn:
            await conn.execute(
                "DELETE FROM source_descriptors WHERE descriptor_id = $1",
                desc_id,
            )
