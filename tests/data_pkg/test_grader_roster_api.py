# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /api/v1/v3/eval/grader_roster`` — the correctness grader's roster (c1).

THROUGH THE REAL ROUTE, on the real driver. The app is wired the way
``server.py`` wires it — ``build_grader_roster_router(deps)`` mounted at
``/api/v1/v3`` — and the rows go into ``unit_references`` / ``unit_correctness``
by direct SQL against the migrated test database (migrations 0196/0197),
mirroring what ``correctness_grader.py`` writes. No mocks for substrate
boundaries, and no reimplementation of the arithmetic under test: the badge the
route emits is compared against ``unit_correctness_api.correctness_badge``
itself, so a divergence between the roster and the Inspector's per-unit badge
is a red test rather than two surfaces quietly printing a NULL share
differently.

What is pinned here, and why:

  * **A NULL correctness share is UNMEASURED, everywhere.** It is counted in
    ``desks_unmeasured``, excluded from ``correctness_mean_of_desks``, and its
    badge says the word. This is the single decision the whole surface exists
    to protect: on the live instance (2026-09-25, desk grain) 173 of 232 desks
    carry a NULL share, so an ``or 0.0`` anywhere in this path would publish a
    roster correctness of 12.5% where the measured figure over the desks that
    HAVE a number is 49.1% — the difference between "the fleet is wrong" and
    "the reference decided nothing for three desks in four".
  * **A coverage of 0.0 is a MEASURED zero** and is averaged like any other
    value. The two absences are different facts and the test seeds both.
  * **Both roster figures, and they differ.** The mean of desks and the
    claim-pooled figure are computed over different populations; the fixtures
    are chosen so the two numbers are not equal, which is the only way a test
    can catch one being served in the other's place.
  * **Latest-per-desk, not all-history.** Two nights per desk go in; the table
    shows the newer, the ``window`` block shows both.
  * **Thinnest coverage first.** The ordering is the section's whole argument.
  * **The two grains are never pooled.** ``country_composition`` is graded
    against the same reference as the desks it composes over, so pooling would
    count those claims twice and average a composition against its own inputs.
    The fixture seeds one composition row and asserts the default roster does
    not see it — and that asking for the composition grain does not see the
    desks.
  * **The route is SELECT-only** and imports under the slim registry image.

This file TRUNCATEs ``unit_correctness`` at setup (the centralized
``clean_tables`` primitive): the roster is a fleet-wide read with no target
filter, so unlike its per-target sibling it cannot scope itself away from rows
another file left behind.
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

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.grader_roster_api import (
    DEFAULT_GRAIN,
    DEFAULT_NIGHTS,
    HONESTY_NOTE,
    MAX_NIGHTS,
    build_grader_roster_router,
)
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.unit_correctness_api import correctness_badge
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = (
    "0011223344556677889900112233445566778899001122334455667788990011"
)
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "44" * 32)

_ROUTE = "/api/v1/v3/eval/grader_roster"

#: A well-formed rubric digest — the column CHECKs ``^[0-9a-f]{64}$``.
RUBRIC_SHA = "0a011222" + "c" * 56

#: The worktree's own ``src``, for the slim-image subprocess probe.
_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")


# ---------------------------------------------------------------------------
# App fixture — the same wiring as `server.py`.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def roster_app(migrated_pg: PostgresConfig, clean_tables):
    os.environ.pop(API_TOKEN_ENV, None)
    # The roster reads the whole table, so it owns the table for its assertions.
    # CASCADE takes `unit_correctness_claims` with it (the FK dependent).
    await clean_tables("unit_correctness")

    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()
    identity = SigningIdentity(
        signing_key=SigningKey(b"c1-grader-roster-route-test-seed"[:32]),
        signer_did="did:legba:registry:grader-roster-test",
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
    # The PRODUCTION mount: the roster route exists only because server.py
    # include_routers this builder under the v3 prefix.
    app.include_router(build_grader_roster_router(deps), prefix="/api/v1/v3")
    yield app, pg_store
    await descriptor_registry.stop()
    await pg_store.close()


@pytest_asyncio.fixture
async def client(roster_app):
    app, _ = roster_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


@pytest.fixture
def pg(roster_app):
    _, pg_store = roster_app
    return pg_store


# ---------------------------------------------------------------------------
# Insertion helpers — mirror `correctness_grader.py`'s own INSERTs.
# ---------------------------------------------------------------------------


async def _insert_reference(
    pg_store: PostgresStore,
    *,
    target_id: str,
    window_start: datetime,
    window_end: datetime,
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
            row_id, target_id, window_start, window_end, window_end,
            "opus-web-lane", json.dumps({"ref_developments": []}),
            0.95, [], uuid4().hex + uuid4().hex,
        )
    return row_id


async def _insert_unit(
    pg_store: PostgresStore,
    *,
    target_id: str,
    reference_id: UUID,
    analyst_id: str,
    as_of: datetime,
    n_contains: int,
    n_contradicts: int,
    n_silent: int,
    n_split: int = 0,
    n_unparseable: int = 0,
    n_single_family: int = 0,
    families: dict[str, Any] | None = None,
    reference_age_days: float | None = 0.0,
    grain: str = "desk",
) -> UUID:
    """One ``unit_correctness`` row with the shares derived the way the grader
    derives them — NULL when the denominator is empty, which is the whole
    point of the fixture."""
    row_id = uuid4()
    n_claims = n_contains + n_contradicts + n_silent + n_split + n_unparseable
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
            0.0, reference_age_days,
        )
    return row_id


#: Two desks over two nights. Desk A is graded on both; desk B's newest night
#: is UNMEASURED (the reference decided nothing) at a MEASURED coverage of 0.0.
#: The numbers are chosen so the mean of desks (0.9) and the pooled figure
#: (0.9 over 10 decided of 52 claims) rest on visibly different denominators,
#: and so the coverage ordering puts the unmeasured desk first.
DESK_A = "escalation"
DESK_B = "economic_stress"


async def _seed_two_desks(pg_store: PostgresStore) -> tuple[str, datetime, datetime]:
    """Seed the canonical fixture; return (target_id, newest as_of, older as_of)."""
    target_id = f"country_g20_c1-{uuid4().hex[:8]}"
    newest = datetime.now(timezone.utc) - timedelta(hours=6)
    older = newest - timedelta(days=1)
    ref = await _insert_reference(
        pg_store,
        target_id=target_id,
        window_start=newest - timedelta(days=7),
        window_end=newest,
    )
    # Desk A: 9/10 decided of 40 claims tonight, 2/4 of 20 last night.
    await _insert_unit(
        pg_store, target_id=target_id, reference_id=ref, analyst_id=DESK_A,
        as_of=newest, n_contains=9, n_contradicts=1, n_silent=30,
    )
    await _insert_unit(
        pg_store, target_id=target_id, reference_id=ref, analyst_id=DESK_A,
        as_of=older, n_contains=2, n_contradicts=2, n_silent=16,
    )
    # Desk B: nothing decided tonight (NULL correctness, 0.0 coverage);
    # 1/1 of 10 last night.
    await _insert_unit(
        pg_store, target_id=target_id, reference_id=ref, analyst_id=DESK_B,
        as_of=newest, n_contains=0, n_contradicts=0, n_silent=12,
    )
    await _insert_unit(
        pg_store, target_id=target_id, reference_id=ref, analyst_id=DESK_B,
        as_of=older, n_contains=1, n_contradicts=0, n_silent=9,
    )
    return target_id, newest, older


def _desk(body: dict[str, Any], analyst_id: str) -> dict[str, Any]:
    for row in body["desks"]:
        if row["analyst_id"] == analyst_id:
            return row
    raise AssertionError(f"{analyst_id} missing from roster: {body['desks']}")


# ---------------------------------------------------------------------------
# The honest empty state
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_roster_is_unavailable_not_zeros(client):
    """Nothing graded reads as nothing graded — no as-of, no invented means."""
    r = await client.get(_ROUTE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is False
    assert body["as_of"] is None
    assert body["desks"] == []
    assert body["nights"] == DEFAULT_NIGHTS
    assert body["grain"] == DEFAULT_GRAIN
    roster = body["roster"]
    assert roster["desks_graded"] == 0
    assert roster["desks_unmeasured"] == 0
    # The MEANS are absent, not zero. A 0.0 here would say the fleet scored
    # zero, which is exactly the lie this surface exists to prevent.
    assert roster["correctness_mean_of_desks"] is None
    assert roster["coverage_mean_of_desks"] is None
    assert roster["claims_pooled"]["correctness_pooled"] is None
    assert roster["claims_pooled"]["coverage_pooled"] is None
    assert roster["claims_pooled"]["n_claims"] == 0
    assert body["honesty_note"] == HONESTY_NOTE


# ---------------------------------------------------------------------------
# Latest-per-desk + the window block
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_latest_row_per_desk_with_the_window_beside_it(client, pg):
    """The table is tonight; the window block is the trailing behaviour."""
    target_id, newest, _older = await _seed_two_desks(pg)

    r = await client.get(_ROUTE)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True
    assert body["as_of"] is not None
    assert len(body["desks"]) == 2, "one row per desk, not one per night"

    a = _desk(body, DESK_A)
    assert a["target_id"] == target_id
    assert a["latest"]["as_of"].startswith(newest.strftime("%Y-%m-%dT%H:%M"))
    assert a["latest"]["correctness_share"] == pytest.approx(0.9)
    assert a["latest"]["coverage_share"] == pytest.approx(0.25)
    assert a["latest"]["n_claims"] == 40
    assert a["latest"]["n_contains"] == 9
    assert a["latest"]["n_decided"] == 10
    assert a["latest"]["n_silent"] == 30
    # Both nights in the window; the trailing means average them.
    assert a["window"]["nights_graded"] == 2
    assert a["window"]["correctness_mean"] == pytest.approx((0.9 + 0.5) / 2)
    assert a["window"]["coverage_mean"] == pytest.approx((0.25 + 0.2) / 2)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_window_excludes_nights_outside_it(client, pg):
    """`nights=1` drops last night — from the table AND from the means."""
    await _seed_two_desks(pg)

    r = await client.get(f"{_ROUTE}?nights=1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["nights"] == 1
    a = _desk(body, DESK_A)
    assert a["window"]["nights_graded"] == 1
    # With one night in the window the trailing mean IS the latest number.
    assert a["window"]["correctness_mean"] == pytest.approx(0.9)
    assert a["latest"]["correctness_share"] == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# The two absences — the decision this whole surface protects
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_null_correctness_is_unmeasured_and_zero_coverage_is_measured(
    client, pg
):
    """A desk the reference decided nothing for is UNMEASURED, not 0% correct —
    and its coverage of 0.0 is a real number, not an absence."""
    await _seed_two_desks(pg)

    body = (await client.get(_ROUTE)).json()
    b = _desk(body, DESK_B)
    assert b["latest"]["correctness_share"] is None, "NULL must survive the hop"
    assert b["latest"]["n_decided"] == 0
    # The coverage is a MEASURED zero: the reference bore on nothing it said.
    assert b["latest"]["coverage_share"] == pytest.approx(0.0)
    assert b["latest"]["n_claims"] == 12
    assert "unmeasured" in b["latest"]["badge"]
    assert "0.0%" not in b["latest"]["badge"].split("·")[0]
    # Postgres' avg() skips the NULL too: the window mean is last night alone.
    assert b["window"]["correctness_mean"] == pytest.approx(1.0)
    assert b["window"]["coverage_mean"] == pytest.approx(0.05)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_roster_means_exclude_the_unmeasured_desk(client, pg):
    """The mean of desks is over the GRADED desks; the unmeasured one is
    counted, named and left out of the numerator AND the denominator."""
    await _seed_two_desks(pg)

    roster = (await client.get(_ROUTE)).json()["roster"]
    assert roster["desks_graded"] == 1
    assert roster["desks_unmeasured"] == 1
    # 0.9 alone — NOT (0.9 + 0)/2 = 0.45, which is what a coalesced NULL gives.
    assert roster["correctness_mean_of_desks"] == pytest.approx(0.9)
    assert roster["correctness_mean_of_desks"] != pytest.approx(0.45)
    # Coverage DOES average both, because both have a coverage number.
    assert roster["coverage_mean_of_desks"] == pytest.approx((0.25 + 0.0) / 2)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pooled_numbers_equal_the_sums_over_the_rendered_rows(client, pg):
    """The pool is the arithmetic of the table, not a second population."""
    await _seed_two_desks(pg)

    body = (await client.get(_ROUTE)).json()
    pooled = body["roster"]["claims_pooled"]
    assert pooled["n_claims"] == sum(d["latest"]["n_claims"] for d in body["desks"])
    assert pooled["n_decided"] == sum(d["latest"]["n_decided"] for d in body["desks"])
    assert pooled["n_contains"] == sum(d["latest"]["n_contains"] for d in body["desks"])
    assert pooled["n_claims"] == 52 and pooled["n_decided"] == 10
    assert pooled["n_contains"] == 9
    assert pooled["correctness_pooled"] == pytest.approx(9 / 10)
    assert pooled["coverage_pooled"] == pytest.approx(10 / 52)
    # The unmeasured desk's 12 claims are IN the pool: they are precisely what
    # makes the pooled coverage thinner than the graded desk's own (10/40), and
    # dropping a desk that decided nothing would flatter the roster.
    assert pooled["coverage_pooled"] < 10 / 40


@pytest.mark.integration
@pytest.mark.asyncio
async def test_both_roster_figures_ship_and_are_distinguishable(client, pg):
    """Mean-of-desks and pooled are separate keys over separate denominators —
    neither can be served in the other's place without the test noticing."""
    await _seed_two_desks(pg)

    roster = (await client.get(_ROUTE)).json()["roster"]
    assert roster["coverage_mean_of_desks"] == pytest.approx(0.125)
    assert roster["claims_pooled"]["coverage_pooled"] == pytest.approx(10 / 52)
    assert roster["coverage_mean_of_desks"] != pytest.approx(
        roster["claims_pooled"]["coverage_pooled"]
    )


# ---------------------------------------------------------------------------
# The badge, the ordering, the bounds
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_badge_is_the_sibling_module_verbatim(client, pg):
    """The roster's badge IS `unit_correctness_api.correctness_badge` — the
    same function the Inspector's per-unit badge renders. Recomputed here from
    the row's own numbers, so a re-implementation on either side goes red."""
    _target, newest, _older = await _seed_two_desks(pg)

    body = (await client.get(_ROUTE)).json()
    for analyst_id, share, coverage, contains, decided, claims in (
        (DESK_A, 0.9, 0.25, 9, 10, 40),
        (DESK_B, None, 0.0, 0, 0, 12),
    ):
        desk = _desk(body, analyst_id)
        expected = correctness_badge(
            correctness_share=share,
            coverage_share=coverage,
            n_contains=contains,
            n_decided=decided,
            n_claims=claims,
            as_of=newest,
            single_family=False,
            reference_state="current",
            reference_age_days=0.0,
        )
        assert desk["latest"]["badge"] == expected
        assert desk["latest"]["reference_state"] == "current"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_desks_are_ordered_thinnest_coverage_first(client, pg):
    """The desks nobody looked at lead the roster. That ordering is the
    section's argument, so it is the server's, not a client preference."""
    await _seed_two_desks(pg)

    body = (await client.get(_ROUTE)).json()
    order = [d["analyst_id"] for d in body["desks"]]
    assert order == [DESK_B, DESK_A], "0.0 coverage must precede 0.25"
    shares = [d["latest"]["coverage_share"] for d in body["desks"]]
    assert shares == sorted(shares)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_stale_reference_travels_on_the_row(client, pg):
    """The grader's STORED age decides the word, and the badge carries it."""
    target_id = f"country_g20_stale-{uuid4().hex[:8]}"
    as_of = datetime.now(timezone.utc) - timedelta(hours=3)
    ref = await _insert_reference(
        pg, target_id=target_id,
        window_start=as_of - timedelta(days=16),
        window_end=as_of - timedelta(days=9),
    )
    await _insert_unit(
        pg, target_id=target_id, reference_id=ref, analyst_id=DESK_A,
        as_of=as_of, n_contains=1, n_contradicts=1, n_silent=8,
        reference_age_days=9.0,
    )

    desk = _desk((await client.get(_ROUTE)).json(), DESK_A)
    assert desk["latest"]["reference_state"] == "stale"
    assert desk["latest"]["reference_age_days"] == pytest.approx(9.0)
    assert "reference stale (9.0 d)" in desk["latest"]["badge"]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("nights", [0, -1, MAX_NIGHTS + 1, 400])
async def test_nights_outside_the_bounds_is_a_400(client, nights):
    r = await client.get(f"{_ROUTE}?nights={nights}")
    assert r.status_code == 400, r.text
    assert f"[1, {MAX_NIGHTS}]" in r.json()["detail"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_route_writes_nothing(client, pg):
    """SELECT-only, proven on the row counts either side of a request."""
    await _seed_two_desks(pg)
    async with pg.acquire() as conn:
        before = await conn.fetchval("SELECT count(*) FROM unit_correctness")
    assert (await client.get(_ROUTE)).status_code == 200
    async with pg.acquire() as conn:
        after = await conn.fetchval("SELECT count(*) FROM unit_correctness")
    assert before == after == 4


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_two_grains_are_never_pooled(client, pg):
    """A composition is graded against the SAME reference as the desks it
    composes over. Pooling the grains would count those claims twice and
    average a composition's number against its own inputs, so the roster is one
    grain at a time and says on the response which one."""
    target_id, newest, _older = await _seed_two_desks(pg)
    async with pg.acquire() as conn:
        ref = await conn.fetchval(
            "SELECT reference_id FROM unit_correctness WHERE target_id = $1 "
            "LIMIT 1", target_id,
        )
    await _insert_unit(
        pg, target_id=target_id, reference_id=ref,
        analyst_id="country_composition", as_of=newest,
        n_contains=40, n_contradicts=0, n_silent=0, grain="composition",
    )

    body = (await client.get(_ROUTE)).json()
    assert body["grain"] == DEFAULT_GRAIN == "desk"
    assert [d["analyst_id"] for d in body["desks"]] == [DESK_B, DESK_A]
    # The composition's 40 contains would have dragged the desk roster from
    # 90% to 98% and its coverage from 19% to 54%. It is not in either.
    assert body["roster"]["claims_pooled"]["n_contains"] == 9
    assert body["roster"]["claims_pooled"]["n_claims"] == 52

    other = (await client.get(f"{_ROUTE}?grain=composition")).json()
    assert other["grain"] == "composition"
    assert [d["analyst_id"] for d in other["desks"]] == ["country_composition"]
    assert other["roster"]["claims_pooled"]["n_claims"] == 40
    assert other["roster"]["desks_graded"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unknown_grain_is_rejected_rather_than_silently_pooled(client):
    """FastAPI's Literal validation is the guard — a typo must not fall back to
    "both", which is the one answer this route may never give."""
    r = await client.get(f"{_ROUTE}?grain=everything")
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# The slim-image guard
# ---------------------------------------------------------------------------


def test_route_imports_without_the_analyst_runtime_stack():
    """The registry image carries no runtime analyst dependencies.

    ``unit_correctness_api`` learned this the hard way: a convenient import of
    ``analysts.deterministic_handlers`` reaches ``feedparser``, and the
    DEPLOYED route answered HTTP 500. This module reuses that module's helpers,
    so it inherits the exposure and needs the same guard. A SUBPROCESS, with
    the blocked modules poisoned to ``None``: the pytest process has the full
    dev stack installed, so an in-process check would pass while production
    burned.

    If this goes red, DO NOT satisfy it by deferring an import — deferring
    moves WHEN the graph is walked, never how far it reaches. Move what the
    route needs into a leaf module, the way
    ``data/correctness_reference_currency`` was.
    """
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore"):
            sys.modules[blocked] = None

        import legba.data.registry.grader_roster_api as roster

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts.deterministic_handlers")
        )
        assert not leaked, "handler package reachable from the route: %r" % leaked
        assert roster.build_grader_roster_router is not None
        assert roster.roster_totals is not None
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, (
        "the roster route no longer imports without the runtime dependency "
        f"stack:\n{result.stdout}\n{result.stderr}"
    )
    assert "OK" in result.stdout


def test_server_mounts_the_roster_router_under_the_v3_prefix():
    """A route that exists as a function but is not mounted is not a route."""
    source = pathlib.Path(_SRC, "legba", "data", "registry", "server.py").read_text(
        encoding="utf-8"
    )
    assert "build_grader_roster_router" in source
    assert (
        'app.include_router(build_grader_roster_router(deps), prefix="/api/v1/v3")'
        in source
    )
