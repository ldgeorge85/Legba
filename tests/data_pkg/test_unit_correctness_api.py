# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for `GET /api/v1/units/{target_id}/correctness` (G2).

THROUGH THE REAL ROUTE. The app is built the way `server.py` builds it —
`build_substrate_reads_router(deps)` mounted at `/api/v1` — and the
correctness route is reached only because that builder `include_router`s the
sibling module. If the mount is ever dropped, every test here 404s, which is
the point: a route that exists as a function but is not mounted is not a
route.

Rows go into `unit_references` / `unit_correctness` / `unit_correctness_claims`
by direct SQL against the migrated test database (migration 0196), mirroring
what `correctness_grader.py` writes. No mocks for substrate boundaries.

What is pinned here, and why:

  * The two shares stay NULL when their denominator is empty — the single most
    consequential column decision in migration 0196. A test that only ever
    inserted a decided unit would let a `or 0.0` creep into hydration and
    nobody would notice until a desk the reference never touched was published
    as 0% correct.
  * The badge string, byte for byte. It is composed server-side precisely so
    there is ONE place to test it.
  * Latest-per-unit, not all-history: the read surface shows today's number.
  * The two grains never pool.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.postgres import PostgresStore
from legba.data.analysts.deterministic_handlers import (
    correctness_grader as GRADER,
)
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.substrate_reads_api import build_substrate_reads_router
from legba.data.registry.unit_correctness_api import (
    MAX_UNIT_LIMIT,
    correctness_badge,
    single_family_row,
)
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = (
    "0011223344556677889900112233445566778899001122334455667788990011"
)
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "33" * 32)

#: A well-formed rubric digest. The column CHECKs `^[0-9a-f]{64}$`, so a
#: placeholder like "sha" would fail the insert rather than the assertion.
RUBRIC_SHA = "0a011222" + "b" * 56

#: The worktree's own `src`, for the subprocess guard below — resolved from
#: this file rather than inherited, so the probe imports the tree under test
#: and not whatever is installed in site-packages.
_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")


def _fixed_identity() -> SigningIdentity:
    seed = b"unit-correctness-api-test-seedxy"
    assert len(seed) == 32
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:unit-correctness-test",
    )


# ---------------------------------------------------------------------------
# App fixture — the same wiring as `server.py`.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def correctness_app(migrated_pg: PostgresConfig):
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

    deps = RegistryAPIDeps(
        descriptor_registry=descriptor_registry,
        stack_registry=StackRegistry(pg_store, vault, audit=audit, dlq=dlq),
        vault=vault,
        dlq=dlq,
        audit_logger=audit,
        vocabulary_cache=vocab,
        nats_store=nats_store,
        conversion_registry=None,
    )

    app = FastAPI()
    app.state.registry_deps = deps
    # The PRODUCTION wiring — the correctness route exists only because the
    # substrate-reads builder mounts it.
    app.include_router(build_substrate_reads_router(deps), prefix="/api/v1")

    yield app, pg_store

    await descriptor_registry.stop()
    await nats_store.close()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(correctness_app):
    app, _ = correctness_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


@pytest.fixture
def pg(correctness_app):
    _, pg_store = correctness_app
    return pg_store


# ---------------------------------------------------------------------------
# Insertion helpers — mirror `correctness_grader.py`'s own INSERTs.
# ---------------------------------------------------------------------------


def _target(label: str) -> str:
    """A per-test unique target so tests never observe each other's rows."""
    return f"uca-{label}-{uuid4().hex[:10]}"


async def _insert_reference(
    pg_store: PostgresStore,
    *,
    target_id: str,
    window_start: datetime,
    window_end: datetime,
    builder: str = "opus-web-lane",
    span_verified_rate: float | None = 0.95,
    thin_dimensions: list[str] | None = None,
) -> UUID:
    row_id = uuid4()
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO unit_references (
                id, target_id, window_start, window_end, built_at, builder,
                ref_json, span_verified_rate, thin_dimensions, sha256
            ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10)
            """,
            row_id, target_id, window_start, window_end, window_end, builder,
            json.dumps({"ref_developments": []}),
            span_verified_rate, thin_dimensions or [],
            uuid4().hex + uuid4().hex,
        )
    return row_id


async def _insert_unit(
    pg_store: PostgresStore,
    *,
    target_id: str,
    reference_id: UUID,
    analyst_id: str,
    as_of: datetime,
    grain: str = "desk",
    n_contains: int = 10,
    n_contradicts: int = 1,
    n_silent: int = 31,
    n_split: int = 2,
    n_unparseable: int = 1,
    n_single_family: int = 0,
    families: dict[str, Any] | None = None,
    cost_usd: float = 0.0,
    reference_age_days: float | None = None,
) -> UUID:
    """One `unit_correctness` row, with the shares derived the way the
    grader derives them — NULL when the denominator is empty."""
    row_id = uuid4()
    n_claims = (
        n_contains + n_contradicts + n_silent + n_split + n_unparseable
    )
    decided = n_contains + n_contradicts
    correctness = (n_contains / decided) if decided else None
    coverage = (decided / n_claims) if n_claims else None
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO unit_correctness (
                id, analyst_id, target_id, head_id, as_of, reference_id,
                rubric_sha, grain, n_claims, n_contains, n_contradicts,
                n_silent, n_split, n_unparseable, n_single_family,
                correctness_share, coverage_share, families, cost_usd,
                reference_age_days
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                $15, $16, $17, $18::jsonb, $19, $20
            )
            """,
            row_id, analyst_id, target_id, uuid4(), as_of, reference_id,
            RUBRIC_SHA, grain, n_claims, n_contains, n_contradicts,
            n_silent, n_split, n_unparseable, n_single_family,
            correctness, coverage,
            json.dumps(families if families is not None else {
                "single_family": False, "F0": {}, "F2": {}, "F3": {},
            }),
            cost_usd, reference_age_days,
        )
    return row_id


async def _insert_claim(
    pg_store: PostgresStore,
    *,
    correctness_id: UUID,
    claim_id: str,
    adjudicated: str = "contains",
    grain: str = "desk",
    claim_text: str = "A claim the reference bore out.",
    label_by_family: dict[str, Any] | None = None,
    n_families: int = 3,
    single_family: bool = False,
    spans: dict[str, Any] | None = None,
) -> UUID:
    row_id = uuid4()
    async with pg_store.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO unit_correctness_claims (
                id, correctness_id, claim_id, grain, claim_text,
                label_by_family, adjudicated, n_families, single_family, spans
            ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10::jsonb)
            """,
            row_id, correctness_id, claim_id, grain, claim_text,
            json.dumps(label_by_family or {
                "F0": "contains", "F2": "contains", "F3": "silent",
            }),
            adjudicated, n_families, single_family,
            json.dumps(spans or {"F0": {"decisive_span": "the span"}}),
        )
    return row_id


def _url(target_id: str, **params: Any) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    base = f"/api/v1/units/{target_id}/correctness"
    return f"{base}?{query}" if query else base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_target_is_empty_with_no_reference(client):
    """A target nobody graded returns NO rows and says why — never a 100%."""
    resp = await client.get(_url(_target("unknown")))
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"] == []
    assert body["next_cursor"] is None
    # The `no reference` state the badge renders in place of a number.
    assert body["reference"]["state"] == "none"
    assert body["reference"]["id"] is None
    assert body["reference"]["age_days"] is None


@pytest.mark.asyncio
async def test_one_desk_row_shape_and_badge(client, pg):
    """The Israel desk aggregate, as Program 1 step 2 measured it."""
    target_id = _target("desk")
    now = datetime.now(timezone.utc)
    ref = await _insert_reference(
        pg, target_id=target_id,
        window_start=now - timedelta(days=14), window_end=now + timedelta(days=1),
        thin_dimensions=["proliferation_watch"],
    )
    as_of = now - timedelta(hours=2)
    await _insert_unit(
        pg, target_id=target_id, reference_id=ref,
        analyst_id="internal_stability", as_of=as_of,
        n_contains=10, n_contradicts=1, n_silent=31, n_split=2,
        n_unparseable=1,
    )

    resp = await client.get(_url(target_id))
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_id"] == target_id
    assert body["reference"]["state"] == "current"
    assert body["reference"]["builder"] == "opus-web-lane"
    assert body["reference"]["thin_dimensions"] == ["proliferation_watch"]

    assert len(body["data"]) == 1
    row = body["data"][0]
    assert row["analyst_id"] == "internal_stability"
    assert row["grain"] == "desk"
    assert row["n_claims"] == 45
    assert row["n_decided"] == 11
    assert row["correctness_share"] == pytest.approx(10 / 11)
    assert row["coverage_share"] == pytest.approx(11 / 45)
    assert row["single_family"] is False
    assert row["reference"]["state"] == "current"
    # The badge, byte for byte — the number a reader actually sees.
    assert row["badge"] == (
        f"correctness 90.9% (10/11) · coverage 24.4% (n=45) "
        f"· as of {as_of.date().isoformat()}"
    )
    # The ledger is NOT attached unless asked for.
    assert row["claims"] is None


@pytest.mark.asyncio
async def test_null_shares_survive_the_route(client, pg):
    """A desk the reference never touched: NULL, not 0.0, all the way out.

    `military_posture` and `proliferation_watch` were exactly this on
    2026-09-16 — 7 and 4 claims, not one of them decided. A `0.0` here would
    publish two desks as wrong about everything.
    """
    target_id = _target("nulls")
    now = datetime.now(timezone.utc)
    ref = await _insert_reference(
        pg, target_id=target_id,
        window_start=now - timedelta(days=14), window_end=now + timedelta(days=1),
    )
    await _insert_unit(
        pg, target_id=target_id, reference_id=ref,
        analyst_id="military_posture", as_of=now - timedelta(hours=1),
        n_contains=0, n_contradicts=0, n_silent=7, n_split=0, n_unparseable=0,
    )

    body = (await client.get(_url(target_id))).json()
    row = body["data"][0]
    assert row["correctness_share"] is None, "a NULL share must not become 0.0"
    assert row["coverage_share"] == pytest.approx(0.0)
    assert row["n_decided"] == 0
    assert "unmeasured" in row["badge"]
    assert "0.0%" not in row["badge"].split("·")[0]


@pytest.mark.asyncio
async def test_latest_row_per_unit_wins(client, pg):
    """Two gradings of one desk: the read surface shows TODAY's number."""
    target_id = _target("latest")
    now = datetime.now(timezone.utc)
    ref = await _insert_reference(
        pg, target_id=target_id,
        window_start=now - timedelta(days=14), window_end=now + timedelta(days=1),
    )
    await _insert_unit(
        pg, target_id=target_id, reference_id=ref, analyst_id="escalation",
        as_of=now - timedelta(days=3),
        n_contains=1, n_contradicts=4, n_silent=1, n_split=0, n_unparseable=0,
    )
    await _insert_unit(
        pg, target_id=target_id, reference_id=ref, analyst_id="escalation",
        as_of=now - timedelta(hours=1),
        n_contains=2, n_contradicts=0, n_silent=4, n_split=0, n_unparseable=0,
    )

    body = (await client.get(_url(target_id))).json()
    assert len(body["data"]) == 1, "one row per unit, the newest"
    assert body["data"][0]["correctness_share"] == pytest.approx(1.0)
    assert body["data"][0]["n_decided"] == 2


@pytest.mark.asyncio
async def test_grains_are_never_pooled(client, pg):
    """Desk and composition come back as separate rows, filterable apart."""
    target_id = _target("grain")
    now = datetime.now(timezone.utc)
    ref = await _insert_reference(
        pg, target_id=target_id,
        window_start=now - timedelta(days=14), window_end=now + timedelta(days=1),
    )
    await _insert_unit(
        pg, target_id=target_id, reference_id=ref, analyst_id="escalation",
        as_of=now, grain="desk",
    )
    await _insert_unit(
        pg, target_id=target_id, reference_id=ref,
        analyst_id="country_composition", as_of=now, grain="composition",
        n_contains=5, n_contradicts=1, n_silent=2, n_split=1, n_unparseable=0,
    )

    body = (await client.get(_url(target_id))).json()
    assert {r["grain"] for r in body["data"]} == {"desk", "composition"}

    comp = (await client.get(_url(target_id, grain="composition"))).json()
    assert len(comp["data"]) == 1
    assert comp["data"][0]["analyst_id"] == "country_composition"
    assert comp["data"][0]["correctness_share"] == pytest.approx(5 / 6)

    one = (await client.get(_url(target_id, analyst_id="escalation"))).json()
    assert [r["analyst_id"] for r in one["data"]] == ["escalation"]


@pytest.mark.asyncio
async def test_claims_ledger_on_demand(client, pg):
    """`?claims=1` attaches the per-claim ledger — the re-argument surface."""
    target_id = _target("claims")
    now = datetime.now(timezone.utc)
    ref = await _insert_reference(
        pg, target_id=target_id,
        window_start=now - timedelta(days=14), window_end=now + timedelta(days=1),
    )
    unit = await _insert_unit(
        pg, target_id=target_id, reference_id=ref,
        analyst_id="internal_stability", as_of=now,
        n_contains=1, n_contradicts=1, n_silent=0, n_split=0, n_unparseable=0,
    )
    await _insert_claim(
        pg, correctness_id=unit, claim_id="DR-aaaaaaaa",
        adjudicated="contains",
    )
    await _insert_claim(
        pg, correctness_id=unit, claim_id="DR-bbbbbbbb",
        adjudicated="contradicts",
        claim_text="A deadlock over Knesset list submissions …",
        label_by_family={
            "F0": "contradicts", "F2": "silent", "F3": "contradicts",
        },
        spans={"F0": {"decisive_span": "38 lists were filed"}},
    )

    body = (await client.get(_url(target_id, claims=1))).json()
    row = body["data"][0]
    assert row["claims"] is not None
    assert len(row["claims"]) == 2
    by_id = {c["claim_id"]: c for c in row["claims"]}
    bad = by_id["DR-bbbbbbbb"]
    assert bad["adjudicated"] == "contradicts"
    assert bad["label_by_family"]["F2"] == "silent"
    assert bad["spans"]["F0"]["decisive_span"] == "38 lists were filed"
    assert bad["n_families"] == 3


@pytest.mark.asyncio
async def test_reference_stale_state(client, pg, monkeypatch):
    """`stale` means what the GRADER means by it, not "past window_end".

    Migration 0197's train made a reference current until
    ``window_end + LEGBA_GRADER_REFERENCE_GRACE_DAYS`` (default 7). A badge
    that called a 2-day-old reference stale would be telling a reader the
    number rests on something expired while the grader was still grading
    against it — the surface explaining a decision nobody made.
    """
    monkeypatch.delenv(GRADER.REFERENCE_GRACE_ENV, raising=False)
    target_id = _target("stale")
    now = datetime.now(timezone.utc)
    window_end = now - timedelta(days=30)
    ref = await _insert_reference(
        pg, target_id=target_id,
        window_start=window_end - timedelta(days=14), window_end=window_end,
    )
    # Inside the grace: current, with a real non-zero age.
    await _insert_unit(
        pg, target_id=target_id, reference_id=ref, analyst_id="inside_grace",
        as_of=window_end + timedelta(days=2), reference_age_days=2.0,
    )
    # Past it: stale.
    await _insert_unit(
        pg, target_id=target_id, reference_id=ref, analyst_id="past_grace",
        as_of=window_end + timedelta(days=9), reference_age_days=9.0,
    )

    body = (await client.get(_url(target_id))).json()
    rows = {r["analyst_id"]: r for r in body["data"]}

    inside = rows["inside_grace"]
    assert inside["reference"]["state"] == "current"
    assert inside["reference"]["age_days"] == pytest.approx(2.0)
    assert "reference stale" not in inside["badge"]

    past = rows["past_grace"]
    assert past["reference"]["state"] == "stale"
    assert past["reference"]["age_days"] == pytest.approx(9.0)
    assert "reference stale (9.0 d)" in past["badge"]

    # The page-level reference is aged at NOW — 30 days out, well past grace.
    assert body["reference"]["state"] == "stale"
    assert body["reference"]["age_days"] == pytest.approx(30.0, abs=0.1)


@pytest.mark.asyncio
async def test_stale_follows_the_operator_grace_env(client, pg, monkeypatch):
    """The bar is the grader's env, read live — one definition, one place."""
    target_id = _target("grace")
    now = datetime.now(timezone.utc)
    window_end = now - timedelta(days=30)
    ref = await _insert_reference(
        pg, target_id=target_id,
        window_start=window_end - timedelta(days=14), window_end=window_end,
    )
    await _insert_unit(
        pg, target_id=target_id, reference_id=ref, analyst_id="escalation",
        as_of=window_end + timedelta(days=4), reference_age_days=4.0,
    )

    monkeypatch.delenv(GRADER.REFERENCE_GRACE_ENV, raising=False)
    body = (await client.get(_url(target_id))).json()
    assert body["data"][0]["reference"]["state"] == "current"

    # The operator tightens it to strict containment.
    monkeypatch.setenv(GRADER.REFERENCE_GRACE_ENV, "0")
    body = (await client.get(_url(target_id))).json()
    assert body["data"][0]["reference"]["state"] == "stale"

    # And widens it.
    monkeypatch.setenv(GRADER.REFERENCE_GRACE_ENV, "30")
    body = (await client.get(_url(target_id))).json()
    assert body["data"][0]["reference"]["state"] == "current"


@pytest.mark.asyncio
async def test_the_stored_reference_age_wins_over_a_re_derivation(client, pg):
    """Migration 0197's column is the RECORD; the derivation is the fallback.

    The grader writes `reference_age_days` from the very stamp it graded at. If
    this route re-derived it from the reference window, a later edit to that
    window would silently change the age a PUBLISHED number is described as
    resting on. So the stored value wins even when it disagrees with the
    window arithmetic, and the row says which it used.
    """
    target_id = _target("stored-age")
    now = datetime.now(timezone.utc)
    window_end = now - timedelta(days=1)
    ref = await _insert_reference(
        pg, target_id=target_id,
        window_start=window_end - timedelta(days=14), window_end=window_end,
    )
    # as_of sits INSIDE the window (derivation would say 0.0), but the grader
    # recorded 11.5 days. The record wins, and it is past the 7-day grace.
    await _insert_unit(
        pg, target_id=target_id, reference_id=ref, analyst_id="stored",
        as_of=window_end - timedelta(hours=1), reference_age_days=11.5,
    )
    # A pre-0197 row: NULL column, aged here by the grader's own helper.
    await _insert_unit(
        pg, target_id=target_id, reference_id=ref, analyst_id="legacy",
        as_of=window_end + timedelta(days=9), reference_age_days=None,
    )

    rows = {
        r["analyst_id"]: r
        for r in (await client.get(_url(target_id))).json()["data"]
    }
    assert rows["stored"]["reference"]["age_days"] == pytest.approx(11.5)
    assert rows["stored"]["reference"]["age_source"] == "stored"
    assert rows["stored"]["reference"]["state"] == "stale"

    assert rows["legacy"]["reference"]["age_days"] == pytest.approx(9.0)
    assert rows["legacy"]["reference"]["age_source"] == "derived"
    assert rows["legacy"]["reference"]["state"] == "stale"


def test_the_route_and_the_grader_share_one_definition_of_stale():
    """Not a behaviour test — a LOCKSTEP test.

    Both sides must reach the SAME function objects in the leaf
    `data/correctness_reference_currency`, so a change to the grace rule cannot
    move one and not the other. Identity, not equality of results: two copies
    that happen to agree today are exactly what this forbids.
    """
    from legba.data import correctness_reference_currency as LEAF
    from legba.data.registry import unit_correctness_api as api

    assert api.reference_age_days is LEAF.reference_age_days
    assert api.reference_grace_days is LEAF.reference_grace_days
    assert api.reference_is_stale is LEAF.reference_is_stale
    # The grader re-exports the same objects, which is what keeps its own
    # tests and `reference_builder.grader_grace_days()` working unchanged.
    assert GRADER.reference_age_days is LEAF.reference_age_days
    assert GRADER.reference_grace_days is LEAF.reference_grace_days
    assert GRADER.REFERENCE_GRACE_ENV == LEAF.REFERENCE_GRACE_ENV
    assert (
        GRADER.DEFAULT_REFERENCE_GRACE_DAYS
        == LEAF.DEFAULT_REFERENCE_GRACE_DAYS
    )

    stamp = datetime(2026, 9, 26, tzinfo=timezone.utc)
    window_end = datetime(2026, 9, 16, tzinfo=timezone.utc)
    assert api._age_days(window_end, stamp) == GRADER.reference_age_days(
        stamp, window_end
    )


def test_the_read_route_imports_without_the_runtime_dependency_stack():
    """THE GUARD. The registry image carries no runtime analyst deps.

    This route module reached `analysts.deterministic_handlers.
    correctness_grader` for the reference-currency rule, whose import graph
    reaches `feedparser`, and the DEPLOYED
    `GET /units/{target_id}/correctness` answered HTTP 500 with
    `ModuleNotFoundError: No module named 'feedparser'`. A deferred import did
    not help and could not: deferring moves WHEN the graph is walked, never HOW
    FAR it reaches.

    So the test runs in a SUBPROCESS with `sys.modules['feedparser']` poisoned
    to `None` — which makes `import feedparser` raise exactly as it does in the
    slim image — and imports the route. A subprocess because the pytest process
    has the full dev stack installed and has very likely already imported the
    handler package, so an in-process check would pass while production burned.

    If this goes red, DO NOT satisfy it by deferring an import. Move whatever
    the route needs into a leaf module with no non-stdlib imports, the way
    `data/correctness_reference_currency` was.
    """
    probe = textwrap.dedent(
        """
        import sys
        # Exactly what the slim image does to these: not present.
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore"):
            sys.modules[blocked] = None

        import legba.data.registry.unit_correctness_api as api
        import legba.data.registry.substrate_reads_api as reads

        # The route answers, and the handler package never entered the graph.
        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts.deterministic_handlers")
        )
        assert not leaked, "handler package reachable from the route: %r" % leaked
        assert api.build_unit_correctness_router is not None
        assert reads.build_substrate_reads_router is not None
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120,
        # Keep the parent's PYTHONPATH tail: in the test container the deps live
        # at /install/lib/python3.11/site-packages, reachable ONLY through it —
        # without this the probe died on `import asyncpg` before reaching the
        # route (2026-09-22), which proved nothing about the slim image.
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, (
        "the read route no longer imports without the runtime dependency "
        f"stack:\n{result.stdout}\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_the_currency_leaf_imports_nothing_but_the_stdlib():
    """The leaf must STAY a leaf — no `legba` import, convenient or not.

    Checked statically on the source, so it fails on the import STATEMENT
    rather than on whatever that statement happens to drag in today.
    """
    import ast
    from legba.data import correctness_reference_currency as LEAF

    tree = ast.parse(pathlib.Path(LEAF.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            # A relative import (level > 0) is a legba import by definition.
            if node.level:
                imported.append(f".{node.module or ''}")
            elif node.module:
                imported.append(node.module.split(".")[0])

    assert set(imported) <= {"__future__", "logging", "os", "datetime", "typing"}, (
        f"{LEAF.__name__} grew a non-stdlib import: {sorted(set(imported))}"
    )


@pytest.mark.asyncio
async def test_single_family_is_surfaced(client, pg):
    """One family's label is a real number, and a weaker one — say so."""
    target_id = _target("single")
    now = datetime.now(timezone.utc)
    ref = await _insert_reference(
        pg, target_id=target_id,
        window_start=now - timedelta(days=14), window_end=now + timedelta(days=1),
    )
    await _insert_unit(
        pg, target_id=target_id, reference_id=ref, analyst_id="escalation",
        as_of=now, n_contains=2, n_contradicts=0, n_silent=3, n_split=0,
        n_unparseable=0, n_single_family=5,
        families={"single_family": True, "F0": {"n_calls": 5}},
    )

    row = (await client.get(_url(target_id))).json()["data"][0]
    assert row["single_family"] is True
    assert row["badge"].endswith("· single-family")


@pytest.mark.asyncio
async def test_pagination_walks_every_unit_once(client, pg):
    """The cursor walks the units without repeating or losing one."""
    target_id = _target("page")
    now = datetime.now(timezone.utc)
    ref = await _insert_reference(
        pg, target_id=target_id,
        window_start=now - timedelta(days=14), window_end=now + timedelta(days=1),
    )
    units = [f"unit_{i:02d}" for i in range(7)]
    for name in units:
        await _insert_unit(
            pg, target_id=target_id, reference_id=ref, analyst_id=name,
            as_of=now,
        )

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        url = _url(target_id, limit=3) + (f"&cursor={cursor}" if cursor else "")
        body = (await client.get(url)).json()
        seen.extend(r["analyst_id"] for r in body["data"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(units)
    assert len(seen) == len(set(seen))


@pytest.mark.asyncio
async def test_limit_bounds_are_enforced(client):
    target_id = _target("limits")
    assert (await client.get(_url(target_id, limit=0))).status_code == 400
    assert (
        await client.get(_url(target_id, limit=MAX_UNIT_LIMIT + 1))
    ).status_code == 400
    assert (await client.get(_url(target_id, claim_limit=0))).status_code == 400
    assert (
        await client.get(_url(target_id, cursor="not-base64"))
    ).status_code == 400


@pytest.mark.asyncio
async def test_route_is_select_only(client, pg):
    """The read surface never writes. Proven by counting rows around a GET."""
    target_id = _target("readonly")
    now = datetime.now(timezone.utc)
    ref = await _insert_reference(
        pg, target_id=target_id,
        window_start=now - timedelta(days=14), window_end=now + timedelta(days=1),
    )
    await _insert_unit(
        pg, target_id=target_id, reference_id=ref, analyst_id="escalation",
        as_of=now,
    )

    async def _counts() -> tuple[int, int, int]:
        async with pg.acquire() as conn:
            return (
                await conn.fetchval("SELECT count(*) FROM unit_references"),
                await conn.fetchval("SELECT count(*) FROM unit_correctness"),
                await conn.fetchval(
                    "SELECT count(*) FROM unit_correctness_claims"
                ),
            )

    before = await _counts()
    assert (await client.get(_url(target_id, claims=1))).status_code == 200
    assert await _counts() == before


# ---------------------------------------------------------------------------
# The two pure helpers the composition gate also depends on.
# ---------------------------------------------------------------------------


def test_single_family_row_predicate():
    """Either route to `single-family` is enough; neither is sufficient alone
    to be ignored. This predicate is SHARED with the composition gate."""
    assert single_family_row({"single_family": True}, 0, 45) is True
    # Every claim carried one family's label, even though the block disagrees.
    assert single_family_row({"single_family": False}, 45, 45) is True
    assert single_family_row({"single_family": False}, 44, 45) is False
    assert single_family_row({}, 0, 0) is False
    # jsonb arrives as a string on some pools.
    assert single_family_row('{"single_family": true}', 0, 9) is True


def test_correctness_badge_never_prints_a_fake_number():
    stamp = datetime(2026, 9, 16, 19, 30, tzinfo=timezone.utc)
    assert correctness_badge(
        correctness_share=10 / 11, coverage_share=11 / 45, n_contains=10,
        n_decided=11, n_claims=45, as_of=stamp, single_family=False,
        reference_state="current", reference_age_days=0.0,
    ) == "correctness 90.9% (10/11) · coverage 24.4% (n=45) · as of 2026-09-16"

    unmeasured = correctness_badge(
        correctness_share=None, coverage_share=0.0, n_contains=0, n_decided=0,
        n_claims=7, as_of=stamp, single_family=False,
        reference_state="current", reference_age_days=None,
    )
    assert unmeasured.startswith("correctness unmeasured (0 decided)")
    assert "100" not in unmeasured
