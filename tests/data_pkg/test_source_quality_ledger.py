# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C3 — the merged source-quality ledger (migration 0115 + the read surface).

Drives the fold against a MIGRATED ephemeral DB (the ``migrated_pg`` fixture —
no mocks for substrate boundaries) to prove:

  * all FOUR legs land in one typed row — asserted Admiralty (0094), asserted
    host credibility (baseline), earned track record (0099), computed A7
    freshness — and are kept APART (no composite score anywhere);
  * the host join follows the SAME exact-then-trimmed rule the signal write
    path uses, including the IP-literal guard (the drift guard that keeps the
    SQL and ``filters.source_credibility.extract_lookup_hosts`` honest);
  * the row spine is every id ANY leg knows — a rating that precedes
    registration, and a track record that outlives a retired descriptor, both
    still produce rows (flagged ``registered=false``);
  * the freshness grade is derived from the source's OWN cadence at read time,
    never stored, never faked to ``ok``;
  * the superseded routes KEEP SERVING their original wire shapes under
    Deprecation/Sunset headers, and the credibility WRITE routes are untouched;
  * **the contention tie-break read is BYTE-IDENTICAL with the ledger present
    and absent** — the review bar for the whole fold (``test_contention_
    tiebreak_read_is_byte_identical_with_and_without_the_ledger``).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.analysts.deterministic_handlers import fact_contention_arbiter as arb
from legba.data.analysts.deterministic_handlers import source_track_record as strk
from legba.data.config import PostgresConfig
from legba.data.filters.source_credibility import extract_lookup_hosts
from legba.data.postgres import PostgresStore
from legba.data.registry.api import (
    API_TOKEN_ENV,
    DEPRECATION_SUNSET_HTTP_DATE,
    RegistryAPIDeps,
    build_router,
)
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.source_assurance_api import build_source_assurance_router
from legba.data.registry.source_credibility_api import (
    build_source_credibility_router,
)
from legba.data.registry.source_quality_api import (
    JUNK_DESCRIPTOR_PREFIXES,
    build_source_quality_router,
)
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "55" * 32)

MIGRATION_0115 = (
    Path(__file__).resolve().parents[2]
    / "src/legba/data/migrations/0115_source_quality_ledger.sql"
)

NOW = datetime(2026, 7, 29, 12, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Substrate seeding
# ---------------------------------------------------------------------------


async def _descriptor(
    conn: Any,
    source_id: str,
    *,
    url: str | None = "https://www.example-wire.test/rss.xml",
    cadence: str | None = "*/15 * * * *",
    state: str = "active",
    kind: str = "rss",
) -> None:
    """One HEAD source descriptor row (the view reads `source_descriptors`)."""
    body: dict[str, Any] = {"identity": {"id": source_id, "state": state}}
    if url is not None:
        body["config"] = {"url": {"raw": url}}
    if cadence is not None:
        body["cadence"] = {"schedule": {"raw": cadence}}
    await conn.execute(
        """
        INSERT INTO source_descriptors
            (descriptor_id, version, schema_uri, is_head, kind, state, owner,
             name, body)
        VALUES ($1, $2, 'legba/source/1.0.0', true, $3, $4, 'test@local', $1,
                $5::jsonb)
        """,
        source_id, uuid4().hex, kind, state, json.dumps(body),
    )


async def _rating(
    conn: Any,
    source_id: str,
    *,
    reliability: str | None = "B",
    credibility: str | None = "2",
    rater: str = "catalog:test",
    visibility: str = "public",
    rated_at: datetime | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO source_ratings
            (source_id, rater, visibility_class, method,
             admiralty_reliability, admiralty_credibility, rubric, refs,
             rated_at)
        VALUES ($1, $2, $3, 'catalog_seed', $4, $5,
                '{"type": "news_agency"}'::jsonb,
                '[{"url": "https://ref.test/x"}]'::jsonb, $6)
        """,
        source_id, rater, visibility, reliability, credibility,
        rated_at or NOW,
    )


async def _dossier(conn: Any, source_id: str) -> None:
    await conn.execute(
        """
        INSERT INTO source_dossiers (source_id, dossier_md, refs, compiled_by)
        VALUES ($1, 'State-funded outlet [1].',
                '[{"url": "https://ref.test/d"}]'::jsonb, 'operator:test')
        """,
        source_id,
    )


async def _host_score(
    conn: Any,
    host: str,
    *,
    score: float = 0.75,
    tier: str = "wire",
    state_affiliation: bool = False,
) -> None:
    await conn.execute(
        """
        INSERT INTO source_credibility
            (source_host, score, score_rationale, scored_by, tier,
             state_affiliation)
        VALUES ($1, $2, 'test rationale', 'test.seed', $3, $4)
        ON CONFLICT (source_host) DO UPDATE
           SET score = EXCLUDED.score, tier = EXCLUDED.tier
        """,
        host, score, tier, state_affiliation,
    )


async def _track_record(
    conn: Any, source_id: str, *, wins: int = 7, losses: int = 3,
) -> None:
    total = wins + losses
    await conn.execute(
        """
        INSERT INTO source_track_records
            (source_id, wins, losses, contested_total, win_rate_raw,
             win_rate_smoothed, win_rate_lower, low_sample, corroborated,
             corroboration_total, corroboration_rate, lag_hours, sample_as_of,
             computed_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, 72, $12, $12)
        """,
        source_id, wins, losses, total,
        (wins / total) if total else None,
        (wins + 2) / (total + 4), 0.42, total < 5,
        wins, total, (wins / total) if total else None,
        NOW,
    )


async def _signals(
    conn: Any, source_id: str, *, count: int = 1, age: timedelta | None = None,
) -> None:
    created = datetime.now(tz=timezone.utc) - (age or timedelta(minutes=1))
    for _ in range(count):
        await conn.execute(
            """
            INSERT INTO signals (id, source_id, created_at, fetched_at)
            VALUES ($1, $2, $3, $3)
            """,
            uuid4(), source_id, created,
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _fixed_identity() -> SigningIdentity:
    return SigningIdentity(
        signing_key=SigningKey(b"c3-source-quality-ledger-seed-01"[:32]),
        signer_did="did:legba:registry:c3-source-quality-test",
    )


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
    app.include_router(build_source_quality_router(deps), prefix="/api/v1/v3")
    app.include_router(build_source_assurance_router(deps), prefix="/api/v1/v3")
    app.include_router(build_source_credibility_router(deps), prefix="/api/v1")

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


@pytest_asyncio.fixture
async def tag(api_app):
    """A per-test id token so rows never collide across the session DB."""
    _, _, pg_store = api_app
    token = uuid4().hex[:10]
    yield token
    async with pg_store.acquire() as conn:
        await conn.execute(
            "DELETE FROM signals WHERE source_id LIKE '%' || $1 || '%'", token,
        )
        for table in (
            "source_track_records", "source_dossiers", "source_ratings",
        ):
            await conn.execute(
                f"DELETE FROM {table} WHERE source_id LIKE '%' || $1 || '%'",
                token,
            )
        await conn.execute(
            "DELETE FROM source_descriptors "
            " WHERE descriptor_id LIKE '%' || $1 || '%'", token,
        )
        await conn.execute(
            "DELETE FROM source_credibility WHERE scored_by = 'test.seed'",
        )


# ---------------------------------------------------------------------------
# The fold: four legs, one typed row
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_four_legs_land_typed_and_unblended(api_app, client, tag):
    """One request answers all four questions, and keeps them apart."""
    _, _, pg_store = api_app
    sid = f"source.full_{tag}"
    async with pg_store.acquire() as conn:
        await _descriptor(conn, sid, url=f"https://www.wire-{tag}.test/rss.xml")
        await _rating(conn, sid, reliability="A", credibility="1")
        await _dossier(conn, sid)
        await _host_score(conn, f"wire-{tag}.test", score=0.83, tier="wire")
        await _track_record(conn, sid, wins=7, losses=3)
        await _signals(conn, sid, count=3)

    r = await client.get(f"/api/v1/v3/sources/{sid}/quality")
    assert r.status_code == 200, r.text
    body = r.json()

    # ASSERTED — both sub-legs, side by side, neither overwriting the other.
    a = body["asserted"]
    assert a["admiralty_grade"] == "A1"
    assert a["admiralty_reliability"] == "A" and a["admiralty_credibility"] == "1"
    assert a["admiralty_rater"] == "catalog:test"
    assert a["public_rating_count"] == 1 and a["private_rating_count"] == 0
    assert a["has_dossier"] is True
    assert a["dossier_compiled_by"] == "operator:test"
    assert a["host_matched"] == f"wire-{tag}.test"
    assert a["host_score"] == pytest.approx(0.83)
    assert a["host_tier"] == "wire"
    assert a["host_state_affiliation"] is False

    # EARNED — the MEASURED leg, untouched by the asserted one.
    e = body["earned"]
    assert e["wins"] == 7 and e["losses"] == 3 and e["contested_total"] == 10
    assert e["win_rate_raw"] == pytest.approx(0.7)
    assert e["low_sample"] is False

    # COMPUTED — derived at read from this source's own cadence.
    c = body["computed"]
    assert c["signals_24h"] == 3 and c["signals_7d"] == 3
    assert c["cadence_raw"] == "*/15 * * * *"
    assert c["budget_minutes"] == 60
    assert c["freshness_grade"] == "ok"

    # MERGED, NOT BLENDED — no composite score exists at any level.
    flat = json.dumps(body)
    assert "quality_score" not in flat and "overall_score" not in flat
    assert set(body) >= {"asserted", "earned", "computed"}
    # The dossier PROSE is detail-only content, present here…
    assert body["dossier"]["dossier_md"].startswith("State-funded")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_row_withholds_dossier_prose(api_app, client, tag):
    """…and absent from the list row, which carries provenance only."""
    _, _, pg_store = api_app
    sid = f"source.listed_{tag}"
    async with pg_store.acquire() as conn:
        await _descriptor(conn, sid)
        await _dossier(conn, sid)

    r = await client.get("/api/v1/v3/source-quality", params={"source_id": tag})
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [row["source_id"] for row in rows] == [sid]
    assert rows[0]["asserted"]["has_dossier"] is True
    assert "dossier" not in rows[0]
    assert "State-funded" not in json.dumps(rows)


# ---------------------------------------------------------------------------
# The host join — the one non-obvious leg
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_view_host_resolution_matches_extract_lookup_hosts(api_app, tag):
    """DRIFT GUARD: the view's SQL host probe == the write path's Python rule.

    For each case the expected answer is COMPUTED from
    ``extract_lookup_hosts`` (first candidate present in the table wins), not
    hand-written — so a change to either implementation that breaks agreement
    fails here rather than silently scoring signals differently from the
    ledger.
    """
    _, _, pg_store = api_app
    cases: list[tuple[str, list[str]]] = [
        # (descriptor host, credibility hosts seeded)
        (f"news.deep.{tag}.test", [f"{tag}.test"]),                 # trimmed
        (f"news.deep.{tag}.test", [f"news.deep.{tag}.test",
                                   f"{tag}.test"]),                 # exact wins
        (f"www.{tag}.test", [f"{tag}.test"]),                       # www strip
        (f"{tag}.test", []),                                        # no match
        ("192.0.2.5", ["0.2.5"]),                                   # IP: no trim
        ("192.0.2.5", ["192.0.2.5"]),                               # IP: exact
    ]

    async with pg_store.acquire() as conn:
        for idx, (host, cred_hosts) in enumerate(cases):
            sid = f"source.host{idx}_{tag}"
            await _descriptor(conn, sid, url=f"https://{host}/feed.xml")
            for ch in cred_hosts:
                await _host_score(conn, ch)

            row = await conn.fetchrow(
                "SELECT endpoint_host, asserted_host_matched "
                "  FROM source_quality WHERE source_id = $1",
                sid,
            )
            # The Python rule, applied to the same table state.
            present = set(cred_hosts)
            expected = next(
                (c for c in extract_lookup_hosts(host) if c in present), None,
            )
            assert row["endpoint_host"] == host, host
            assert row["asserted_host_matched"] == expected, (
                f"host={host!r} seeded={cred_hosts!r}: view chose "
                f"{row['asserted_host_matched']!r}, extract_lookup_hosts "
                f"implies {expected!r}"
            )
            # Clean between cases so a later case cannot inherit a match.
            await conn.execute(
                "DELETE FROM source_credibility WHERE scored_by = 'test.seed'",
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_endpoint_url_leaves_the_host_leg_empty(api_app, tag):
    """A source with no declared endpoint has no host to score — null, not a
    guess (the ledger never invents a leg it cannot resolve)."""
    _, _, pg_store = api_app
    sid = f"source.nourl_{tag}"
    async with pg_store.acquire() as conn:
        await _descriptor(conn, sid, url=None)
        await _host_score(conn, f"{tag}.test")
        row = await conn.fetchrow(
            "SELECT endpoint_host, asserted_host_matched, asserted_host_score "
            "  FROM source_quality WHERE source_id = $1",
            sid,
        )
    assert row["endpoint_host"] is None
    assert row["asserted_host_matched"] is None
    assert row["asserted_host_score"] is None


# ---------------------------------------------------------------------------
# The row spine — every id ANY leg knows
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rating_before_registration_still_has_a_row(api_app, client, tag):
    """Catalog seeds legitimately rate a source BEFORE it is registered."""
    _, _, pg_store = api_app
    sid = f"source.unregistered_{tag}"
    async with pg_store.acquire() as conn:
        await _rating(conn, sid, reliability="C", credibility="3")

    r = await client.get(f"/api/v1/v3/sources/{sid}/quality")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["registered"] is False
    assert body["declared_state"] is None
    assert body["asserted"]["admiralty_grade"] == "C3"
    assert body["earned"] is None
    assert body["computed"]["freshness_grade"] == "ungraded"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_track_record_outlives_a_retired_descriptor(api_app, client, tag):
    """A record may exist for a source whose descriptor is gone (0099's
    no-FK posture) — the ledger keeps showing it rather than losing history."""
    _, _, pg_store = api_app
    sid = f"source.retired_{tag}"
    async with pg_store.acquire() as conn:
        await _track_record(conn, sid, wins=1, losses=2)

    r = await client.get(f"/api/v1/v3/sources/{sid}/quality")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["registered"] is False
    assert body["earned"]["losses"] == 2
    # Below the 0099 sample floor (contested_total < 5) — thin evidence is
    # labelled as such rather than presented as a rate.
    assert body["earned"]["low_sample"] is True
    assert body["asserted"]["admiralty_grade"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_detail_404_when_no_leg_knows_the_id(client):
    r = await client.get(f"/api/v1/v3/sources/source.never_{uuid4().hex}/quality")
    assert r.status_code == 404
    assert "no descriptor, ratings, dossier, or earned record" in r.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_known_but_empty_source_is_shown_not_hidden(api_app, client, tag):
    """A registered source nobody rated and nothing measured still appears with
    every leg empty — "we know nothing" must be distinguishable from absent."""
    _, _, pg_store = api_app
    sid = f"source.blank_{tag}"
    async with pg_store.acquire() as conn:
        await _descriptor(conn, sid, url=None, cadence=None)

    r = await client.get("/api/v1/v3/source-quality", params={"source_id": tag})
    assert [row["source_id"] for row in r.json()] == [sid]
    row = r.json()[0]
    assert row["registered"] is True
    assert row["asserted"]["admiralty_grade"] is None
    assert row["asserted"]["host_score"] is None
    assert row["earned"] is None
    assert row["computed"]["freshness_grade"] == "ungraded"
    assert row["computed"]["budget_minutes"] is None


# ---------------------------------------------------------------------------
# The computed leg — graded from the source's OWN cadence, at read
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cadence,state,age,signal_count,expected",
    [
        ("*/15 * * * *", "active", timedelta(minutes=5), 1, "ok"),
        ("*/15 * * * *", "active", timedelta(hours=2), 1, "stale"),
        ("*/15 * * * *", "active", timedelta(days=3), 1, "warn"),
        ("*/15 * * * *", "active", None, 0, "empty"),
        (None, "active", timedelta(minutes=5), 1, "ungraded"),
        ("*/15 * * * *", "paused", timedelta(minutes=5), 1, "ungraded"),
    ],
)
async def test_freshness_grade_uses_the_sources_own_cadence(
    api_app, client, tag, cadence, state, age, signal_count, expected,
):
    _, _, pg_store = api_app
    sid = f"source.fresh_{tag}"
    async with pg_store.acquire() as conn:
        await _descriptor(conn, sid, cadence=cadence, state=state)
        if signal_count:
            await _signals(conn, sid, count=signal_count, age=age)

    r = await client.get(f"/api/v1/v3/sources/{sid}/quality")
    assert r.status_code == 200, r.text
    assert r.json()["computed"]["freshness_grade"] == expected


# ---------------------------------------------------------------------------
# List behaviour
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_excludes_junk_template_descriptors(api_app, client, tag):
    """The same junk list /v3/system/source-firing uses — the VIEW keeps them
    (a ledger that drops rows is not a ledger), the READ filters them."""
    _, _, pg_store = api_app
    junk_id = f"{JUNK_DESCRIPTOR_PREFIXES[0]}{tag}"
    real_id = f"source.real_{tag}"
    async with pg_store.acquire() as conn:
        await _descriptor(conn, junk_id)
        await _descriptor(conn, real_id)
        in_view = await conn.fetchval(
            "SELECT count(*) FROM source_quality WHERE source_id = $1", junk_id,
        )
    assert in_view == 1, "the view itself must still carry the row"

    r = await client.get("/api/v1/v3/source-quality", params={"source_id": tag})
    assert [row["source_id"] for row in r.json()] == [real_id]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_filters_graded_contested_and_freshness(api_app, client, tag):
    _, _, pg_store = api_app
    graded = f"source.graded_{tag}"
    contested = f"source.contested_{tag}"
    plain = f"source.plain_{tag}"
    async with pg_store.acquire() as conn:
        await _descriptor(conn, graded)
        await _rating(conn, graded)
        await _descriptor(conn, contested)
        await _track_record(conn, contested, wins=6, losses=2)
        await _descriptor(conn, plain, cadence=None)

    async def ids(**params: Any) -> list[str]:
        resp = await client.get(
            "/api/v1/v3/source-quality", params={"source_id": tag, **params},
        )
        assert resp.status_code == 200, resp.text
        return sorted(row["source_id"] for row in resp.json())

    assert await ids() == sorted([graded, contested, plain])
    assert await ids(graded_only=True) == [graded]
    assert await ids(contested_only=True) == [contested]
    assert await ids(freshness_grade="ungraded") == [plain]
    # Contested sources sort FIRST — the ones with a measured record.
    resp = await client.get(
        "/api/v1/v3/source-quality", params={"source_id": tag},
    )
    assert resp.json()[0]["source_id"] == contested


@pytest.mark.integration
@pytest.mark.asyncio
async def test_private_annex_ratings_only_on_explicit_optin(api_app, client, tag):
    """Default-deny is preserved by the merged surface: private CONTENT needs
    the opt-in, while the private COUNT is always visible (knowing an annex
    rating exists leaks nothing)."""
    _, _, pg_store = api_app
    sid = f"source.vis_{tag}"
    async with pg_store.acquire() as conn:
        await _descriptor(conn, sid)
        await _rating(conn, sid, rater="catalog:public", visibility="public")
        await _rating(
            conn, sid, rater="annex:acme", visibility="private",
            reliability="F", credibility="6",
        )

    r = await client.get(f"/api/v1/v3/sources/{sid}/quality")
    body = r.json()
    assert body["includes_private"] is False
    assert [x["rater"] for x in body["ratings"]] == ["catalog:public"]
    assert body["asserted"]["public_rating_count"] == 1
    assert body["asserted"]["private_rating_count"] == 1
    # The headline grade NEVER comes from a private row.
    assert body["asserted"]["admiralty_grade"] == "B2"

    r2 = await client.get(
        f"/api/v1/v3/sources/{sid}/quality", params={"include_private": 1},
    )
    body2 = r2.json()
    assert body2["includes_private"] is True
    assert sorted(x["rater"] for x in body2["ratings"]) == [
        "annex:acme", "catalog:public",
    ]
    assert body2["asserted"]["admiralty_grade"] == "B2"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_superseded_ratings_are_not_current(api_app, client, tag):
    """Only un-superseded rows count — the 0094 supersession chain is history,
    not a second opinion."""
    _, _, pg_store = api_app
    sid = f"source.super_{tag}"
    async with pg_store.acquire() as conn:
        await _descriptor(conn, sid)
        await _rating(conn, sid, reliability="D", credibility="4")
        old_id = await conn.fetchval(
            "SELECT id FROM source_ratings WHERE source_id = $1", sid,
        )
        await conn.execute(
            "UPDATE source_ratings SET superseded_by = id WHERE id = $1", old_id,
        )
    r = await client.get(f"/api/v1/v3/sources/{sid}/quality")
    body = r.json()
    assert body["asserted"]["admiralty_grade"] is None
    assert body["asserted"]["public_rating_count"] == 0
    assert body["ratings"] == []


# ---------------------------------------------------------------------------
# Deprecation window — the old routes KEEP SERVING
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_superseded_reads_keep_serving_with_sunset_headers(
    api_app, client, tag,
):
    """No 3xx, no shape change — the original bodies, plus the headers that
    announce the window. A redirect would hand callers a different body."""
    _, _, pg_store = api_app
    sid = f"source.dep_{tag}"
    host = f"dep-{tag}.test"
    async with pg_store.acquire() as conn:
        await _descriptor(conn, sid, url=f"https://{host}/rss.xml")
        await _rating(conn, sid)
        await _host_score(conn, host, score=0.66)

    assurance = await client.get(f"/api/v1/v3/sources/{sid}/assurance")
    assert assurance.status_code == 200
    assert assurance.json()["assurance_grade"] == "B2"
    assert assurance.json()["ratings"][0]["rater"] == "catalog:test"

    cred_list = await client.get(
        "/api/v1/source_credibility", params={"host": host},
    )
    assert cred_list.status_code == 200
    assert cred_list.json()[0]["score"] == pytest.approx(0.66)

    cred_one = await client.get(f"/api/v1/source_credibility/{host}")
    assert cred_one.status_code == 200

    for resp, successor in (
        (assurance, "/api/v1/v3/sources/{source_id}/quality"),
        (cred_list, "/api/v1/v3/source-quality"),
        (cred_one, "/api/v1/v3/source-quality"),
    ):
        assert resp.headers["Deprecation"] == "true"
        assert resp.headers["Sunset"] == DEPRECATION_SUNSET_HTTP_DATE
        assert resp.headers["Link"] == f'<{successor}>; rel="successor-version"'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_credibility_write_routes_are_not_deprecated(client, tag):
    """The ledger is a READ surface — it has no successor for the writes, so
    they must not be swept into the window."""
    host = f"writes-{tag}.test"
    put = await client.put(
        f"/api/v1/source_credibility/{host}",
        json={"score": 0.5, "score_rationale": "t", "scored_by": "test.seed"},
    )
    assert put.status_code == 200
    assert "Deprecation" not in put.headers
    assert "Sunset" not in put.headers

    delete = await client.delete(f"/api/v1/source_credibility/{host}")
    assert delete.status_code == 200
    assert "Deprecation" not in delete.headers


@pytest.mark.integration
@pytest.mark.asyncio
async def test_new_surface_is_not_itself_deprecated(api_app, client, tag):
    _, _, pg_store = api_app
    sid = f"source.fresh_route_{tag}"
    async with pg_store.acquire() as conn:
        await _descriptor(conn, sid)
    for path in ("/api/v1/v3/source-quality", f"/api/v1/v3/sources/{sid}/quality"):
        resp = await client.get(path)
        assert resp.status_code == 200
        assert "Deprecation" not in resp.headers


# ---------------------------------------------------------------------------
# THE REVIEW BAR — contention tie-break reads are byte-identical
# ---------------------------------------------------------------------------


async def _seed_resolved_contention(conn: Any, tag: str) -> UUID:
    """One RESOLVED contention with a winner (2 sources) and a loser."""
    sw, sc, sl = (
        f"source.win_{tag}", f"source.corr_{tag}", f"source.lose_{tag}",
    )
    sig_ids: dict[str, UUID] = {}
    for src in (sw, sc, sl):
        sid = uuid4()
        await conn.execute(
            "INSERT INTO signals (id, source_id) VALUES ($1, $2)", sid, src,
        )
        sig_ids[src] = sid

    async def _fact(value: str, sig: UUID, seq: int) -> UUID:
        fid = uuid4()
        await conn.execute(
            """
            INSERT INTO facts (id, subject, predicate, value, confidence,
                               valid_from, derived_from)
            VALUES ($1, $2, 'border status', $3, 0.7, $4, $5::uuid[])
            """,
            fid, f"Subject-{tag}", value, NOW - timedelta(minutes=seq), [sig],
        )
        return fid

    w1 = await _fact("de-escalating", sig_ids[sw], 0)
    w2 = await _fact("de-escalating", sig_ids[sc], 1)
    l1 = await _fact("clashes ongoing", sig_ids[sl], 2)

    cid = uuid4()
    await conn.execute(
        """
        INSERT INTO fact_contention
            (id, subject_key, predicate_key, status, surfaced_value,
             surfaced_fact_id, surfaced_by, surfaced_at, opened_at)
        VALUES ($1, $2, 'border status', 'surfaced', 'de-escalating', $3,
                'deterministic', $4, $4)
        """,
        cid, f"subject-{tag}", w1, NOW - timedelta(hours=100),
    )
    for value_key, facts, winner, dsc in (
        ("de-escalating", [w1, w2], True, 2),
        ("clashes ongoing", [l1], False, 1),
    ):
        await conn.execute(
            """
            INSERT INTO fact_contention_values
                (contention_id, value_key, representative_fact_id,
                 distinct_source_count, supporting_fact_ids, surfaced_winner,
                 is_junk)
            VALUES ($1, $2, $3, $4, $5::uuid[], $6, false)
            """,
            cid, value_key, facts[0], dsc, facts, winner,
        )
    return cid


async def _contention_read_bytes(conn: Any, cid: UUID, tag: str) -> bytes:
    """Canonical serialization of EVERYTHING the tie-break path reads.

    Exercised with the earned seam explicitly ON (the OFF path short-circuits
    before touching the substrate, so it could not detect an interference the
    ON path would suffer).
    """
    sources = [f"source.win_{tag}", f"source.corr_{tag}", f"source.lose_{tag}"]
    records = await strk.compute_source_records(conn, now=NOW, lag_hours=72.0)
    weights_all = await strk.earned_weights_for_sources(
        conn, sources, now=NOW, exclude_contention=None, lag_hours=72.0,
    )
    weights_excl = await strk.earned_weights_for_sources(
        conn, sources, now=NOW, exclude_contention=cid, lag_hours=72.0,
    )

    rows = await conn.fetch(
        """
        SELECT value_key, supporting_fact_ids, distinct_source_count
          FROM fact_contention_values
         WHERE contention_id = $1
         ORDER BY value_key
        """,
        cid,
    )
    aggs: list[arb._ValueAgg] = []
    for r in rows:
        agg = arb._ValueAgg(r["value_key"])
        agg.supporting_fact_ids = list(r["supporting_fact_ids"])
        agg.distinct_lineage = {str(f) for f in r["supporting_fact_ids"]}
        agg.source_types = {"news"}
        agg.cred_sum = 0.5 * len(r["supporting_fact_ids"])
        aggs.append(agg)
    await arb._attach_earned_weights(conn, aggs, contention_id=cid, now=NOW)

    payload = {
        "records": [
            {
                "source_id": rec.source_id,
                "wins": rec.wins,
                "losses": rec.losses,
                "contested_total": rec.contested_total,
                "win_rate_raw": rec.win_rate_raw,
                "win_rate_smoothed": rec.win_rate_smoothed,
                "win_rate_lower": rec.win_rate_lower,
                "low_sample": rec.low_sample,
                "corroborated": rec.corroborated,
                "corroboration_total": rec.corroboration_total,
                "earned_signal": rec.earned_signal,
            }
            for rec in sorted(records, key=lambda x: x.source_id)
        ],
        "weights_all": dict(sorted(weights_all.items())),
        "weights_excluding_self": dict(sorted(weights_excl.items())),
        "attached_earned_weight": {a.value_key: a.earned_weight for a in aggs},
        "tiebreak_weights": arb._tiebreak_weights(aggs),
    }
    return json.dumps(payload, sort_keys=True).encode("utf-8")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_contention_tiebreak_read_is_byte_identical_with_and_without_the_ledger(
    api_app, tag, monkeypatch,
):
    """**The C3 review bar.** The earned tie-break read must be unchanged by
    the fold.

    Proven by execution, not inspection: the full read is serialized
    canonically with the ledger view PRESENT, the view is then DROPPED, and the
    identical read is serialized again. Byte equality means the ledger cannot
    influence the arbiter — which is the whole point of layer 3 being
    recomputed live under the acyclicity guard rather than read from a stored
    (or merged) surface.
    """
    monkeypatch.setenv(arb.EARNED_WEIGHT_ENV, "1")
    _, _, pg_store = api_app
    view_sql = MIGRATION_0115.read_text()

    async with pg_store.acquire() as conn:
        cid = await _seed_resolved_contention(conn, tag)
        with_ledger = await _contention_read_bytes(conn, cid, tag)
        # Sanity: the fixture actually exercises the earned path (an all-zero
        # read would make byte equality vacuous).
        assert b'"wins": 1' in with_ledger
        assert await conn.fetchval("SELECT count(*) FROM source_quality") >= 0

        try:
            await conn.execute("DROP VIEW public.source_quality")
            without_ledger = await _contention_read_bytes(conn, cid, tag)
        finally:
            await conn.execute(view_sql)

        # And once more with the ledger restored — no hysteresis either.
        restored = await _contention_read_bytes(conn, cid, tag)

    assert with_ledger == without_ledger
    assert restored == with_ledger

    # The seam is also structurally independent: nothing in the tie-break path
    # names the ledger surface.
    for mod in (arb, strk):
        src = Path(mod.__file__).read_text()
        assert "source_quality" not in src, (
            f"{mod.__name__} must not read the C3 ledger — the earned weight "
            "recomputes live under the acyclicity guard"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_earned_seam_stays_off_by_default_after_the_fold(api_app, tag):
    """The OFF invariant survives the merge: with the flag unset the tie-break
    weight is exactly the P3-2 formula, ledger or no ledger."""
    _, _, pg_store = api_app
    async with pg_store.acquire() as conn:
        await _seed_resolved_contention(conn, tag)
    agg = arb._ValueAgg("v")
    agg.distinct_lineage = {"a", "b"}
    agg.source_types = {"news", "gov"}
    agg.cred_sum = 1.5
    agg.earned_weight = 0.9      # populated, and still ignored
    assert arb._earned_track_record_weight(agg) == 0.0
    assert arb._tiebreak_weight(agg) == pytest.approx(
        float(agg.distinct_source_count) + 2.0 + 1.5
    )


# ---------------------------------------------------------------------------
# Structural / degradation
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_routes_refuse_loud_when_the_view_is_missing(api_app, client, tag):
    """A registry rolled forward ahead of 0115 must SAY the ledger is
    unavailable (503), not serve an empty list that reads as "no sources"."""
    _, _, pg_store = api_app
    view_sql = MIGRATION_0115.read_text()
    async with pg_store.acquire() as conn:
        await conn.execute("DROP VIEW public.source_quality")
    try:
        listed = await client.get("/api/v1/v3/source-quality")
        detail = await client.get("/api/v1/v3/sources/source.x/quality")
    finally:
        async with pg_store.acquire() as conn:
            await conn.execute(view_sql)
    assert listed.status_code == 503
    assert "migration 0115" in listed.text
    assert detail.status_code == 503


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_is_idempotent(api_app, tag):
    """Re-applying 0115 is a no-op (CREATE OR REPLACE, no data touched)."""
    _, _, pg_store = api_app
    sid = f"source.idem_{tag}"
    view_sql = MIGRATION_0115.read_text()
    async with pg_store.acquire() as conn:
        await _descriptor(conn, sid)
        before = await conn.fetchrow(
            "SELECT * FROM source_quality WHERE source_id = $1", sid,
        )
        await conn.execute(view_sql)
        after = await conn.fetchrow(
            "SELECT * FROM source_quality WHERE source_id = $1", sid,
        )
    assert dict(before) == dict(after)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ledger_carries_no_composite_score_column(api_app):
    """Schema-level guarantee of the honesty split: every non-backbone column
    is prefixed with the KIND of knowledge it carries, and nothing aggregates
    across kinds."""
    _, _, pg_store = api_app
    async with pg_store.acquire() as conn:
        cols = [
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                " WHERE table_schema = 'public' AND table_name = 'source_quality'"
            )
        ]
    backbone = {
        "source_id", "registered", "declared_state", "declared_kind",
        "cadence_raw", "endpoint_url", "endpoint_host",
    }
    typed = [c for c in cols if c not in backbone]
    assert typed, "the view must carry typed leg columns"
    for col in typed:
        assert col.startswith(("asserted_", "earned_", "computed_")), col
    assert not any("score" == c or c.endswith("_quality_score") for c in cols)
