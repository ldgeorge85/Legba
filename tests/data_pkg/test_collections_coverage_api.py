# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/collections/coverage?scope=`` and the ``history_gap`` absence (7g-2).

The era coverage map answers the question a reader of a cited historical number
actually has — *what years does the platform hold for this desk, and where are
the holes* — and the ``history_gap`` typed absence is the same measurement
typed. Both bind through the REAL ASGI app over a migrated test DB.

What is pinned here, and why each one:

  * a hole the manifest DECLARES and the table LACKS is a gap, proved by the
    LOAD receipt that should have written it;
  * a year the PROVIDER never published is **not** a gap — the manifest records
    that in the provider's own words, and restating it as ours moves the blame
    and loses the reason;
  * a desk NO loaded holding names is **not** a gap either: it goes to
    ``not_measured`` naming the desks the holdings do cover, because a gap in a
    holding that does not exist is not an absence this platform can type;
  * a holding still in ``reviewed`` is invisible to both surfaces — the
    operator's approval is the gate;
  * the two surfaces read the SAME measurement, so they cannot disagree about
    the same silence;
  * the slim-image import guard — this route ships in the registry image.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.absence_api import build_absence_router
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps
from legba.data.registry.audit import AuditLogger
from legba.data.registry.collections_api import build_collections_router
from legba.data.registry.collections_coverage import (
    COLLECTIONS_COVERAGE_VERSION,
    GAP_SHELF_LIFE_DAYS,
    HISTORY_GAP_KIND,
    year_spans,
)
from legba.data.registry.credentials import CredentialVault, MASTER_KEY_ENV
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "77" * 32)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SRC = str(_REPO_ROOT / "src")
_ROUTE = "/api/v1/v3/collections/coverage"
_ABSENCE = "/api/v1/v3/absence"

_TAG = "t7g2cov"
_COLLECTION = f"collection.{_TAG}_pilot"
#: Desk ids unique to this module — every table it touches is shared.
DESK = f"country_watch_{_TAG}"
BARE_DESK = f"country_watch_{_TAG}_bare"
UNKNOWN_DESK = f"country_watch_{_TAG}_nowhere"

#: The manifest this module registers as the holding's body. Three series, one
#: subject, and a declared hole the loader did not fill (`wb.cpi` 2018 is
#: missing from the table) beside one the PROVIDER never published.
_BODY = {
    "subject_kind": "country",
    "subjects": [
        {"desk": DESK, "subject": "IR", "name": "Iran"},
        {"desk": BARE_DESK, "subject": "ZZ", "name": "Nowhere"},
    ],
    "window": {"valid_from": "2016-01-01", "valid_to": "2018-12-31"},
    "manifest": {
        "series": [
            {
                "series_id": "wb.gdp",
                "provider": "world_bank",
                "indicator_name": "GDP growth (annual %)",
                "unit": "pct_per_year",
                "cadence": "annual",
                "subjects": ["IR", "ZZ"],
                "valid_from": "2016-01-01",
                "valid_to": "2018-12-31",
                "coverage": {
                    "fetched_at": "2026-09-24",
                    "provider_last_updated": "2026-07-13",
                    "IR": {
                        "values": 3, "nulls": 0,
                        "first_valid_year": 2016, "last_valid_year": 2018,
                    },
                    "ZZ": {
                        "values": 3, "nulls": 0,
                        "first_valid_year": 2016, "last_valid_year": 2018,
                    },
                },
            },
            {
                "series_id": "wb.cpi",
                "provider": "world_bank",
                "indicator_name": "Inflation, consumer prices (annual %)",
                "unit": "pct_per_year",
                "cadence": "annual",
                "subjects": ["IR"],
                "valid_from": "2016-01-01",
                "valid_to": "2018-12-31",
                "coverage": {
                    "fetched_at": "2026-09-24",
                    "IR": {
                        "values": 3, "nulls": 0,
                        "first_valid_year": 2016, "last_valid_year": 2018,
                    },
                },
            },
            {
                "series_id": "wb.debt",
                "provider": "world_bank",
                "indicator_name": "External debt stocks",
                "unit": "current_usd",
                "cadence": "annual",
                "subjects": ["IR"],
                "valid_from": "2016-01-01",
                "valid_to": "2018-12-31",
                "coverage": {
                    "fetched_at": "2026-09-24",
                    "IR": {
                        "values": 0, "nulls": 3, "held": False,
                        "reason": "the Debtor Reporting System does not cover this subject",
                    },
                },
            },
        ]
    },
}


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)
    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()
    await _purge(pg_store)
    identity = SigningIdentity(
        signing_key=SigningKey(b"7g2-coverage-route-test-seed-01!"[:32]),
        signer_did="did:legba:registry:7g2-coverage-test",
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
    app.include_router(build_collections_router(deps), prefix="/api/v1/v3")
    app.include_router(build_absence_router(deps), prefix="/api/v1/v3")
    yield app, pg_store
    await _purge(pg_store)
    await descriptor_registry.stop()
    await pg_store.close()


async def _purge(pg_store: PostgresStore) -> None:
    async with pg_store.acquire() as conn:
        await conn.execute(
            "DELETE FROM public.observations WHERE collection_id LIKE $1",
            f"collection.{_TAG}%",
        )
        await conn.execute(
            "DELETE FROM public.collection_loads WHERE collection_id LIKE $1",
            f"collection.{_TAG}%",
        )
        await conn.execute(
            "DELETE FROM public.collection_descriptors WHERE descriptor_id LIKE $1",
            f"collection.{_TAG}%",
        )


@pytest_asyncio.fixture
async def client(api_app):
    app, _ = api_app
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as c:
        yield c


_LOADED_AT = datetime(2026, 9, 25, 20, 40, 31, tzinfo=timezone.utc)


async def _seed(
    pg_store: PostgresStore,
    *,
    state: str = "loaded",
    with_rows: bool = True,
    finished_at: datetime | None = _LOADED_AT,
) -> str:
    """Register the holding and (optionally) the rows a load would have written.

    ``wb.gdp`` is complete for IR (2016-2018). ``wb.cpi`` is missing 2018 —
    the LOAD GAP. ``wb.debt`` is declared held:false — the provider's own
    absence, which must never read as our gap.
    """
    load_id = str(uuid4())
    async with pg_store.acquire() as conn:
        await conn.execute(
            "INSERT INTO collection_descriptors "
            "(descriptor_id, version, schema_uri, abstraction_level, state, "
            " owner, name, body, is_head, origin_shape, origin_class, "
            " licence_class, collection_version) "
            "VALUES ($1, $2, 'legba/collection/1.0.0', 'L0', $3, 'test_7g2', "
            "'coverage under test', $4::jsonb, TRUE, 'archive_only', "
            "'archive', 'public', 'v1')",
            _COLLECTION, "0" * 64, state, json.dumps(_BODY),
        )
        await conn.execute(
            "INSERT INTO collection_loads "
            "(id, collection_id, collection_version, loader_kind, status, "
            " pairs_total, pairs_done, rows_written, finished_at) "
            "VALUES ($1::uuid, $2, 'v1', 'series_api', 'completed', 4, 4, 5, $3)",
            load_id, _COLLECTION, finished_at,
        )
        if not with_rows:
            return load_id
        rows = [("wb.gdp", y) for y in (2016, 2017, 2018)]
        rows += [("wb.cpi", y) for y in (2016, 2017)]  # 2018 missing = the gap
        for series_id, year in rows:
            await conn.execute(
                "INSERT INTO observations "
                "(collection_id, series_id, subject_kind, subject, valid_from, "
                " valid_to, record_time, value, unit, source_url, sha256, "
                " origin_class) "
                "VALUES ($1, $2, 'country', 'IR', $3, $4, $5, $6, "
                "'pct_per_year', 'https://example.invalid/x', 'sha', 'archive')",
                _COLLECTION, series_id, date(year, 1, 1), date(year, 12, 31),
                datetime(2026, 7, 13, tzinfo=timezone.utc), Decimal("1.5"),
            )
    return load_id


# ---------------------------------------------------------------------------
# The pure projection
# ---------------------------------------------------------------------------


def test_year_spans_collapses_contiguous_runs_losslessly():
    assert [(s.from_year, s.to_year) for s in year_spans([2016, 2017, 2018, 2020])] == [
        (2016, 2018), (2020, 2020),
    ]
    assert year_spans([]) == []


# ---------------------------------------------------------------------------
# The coverage route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_blank_scope_is_422_naming_the_parameter(client):
    r = await client.get(_ROUTE, params={"scope": "   "})
    assert r.status_code == 422
    assert "scope" in r.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_map_shows_the_spans_held_and_the_holes(api_app, client):
    _, pg_store = api_app
    await _seed(pg_store)
    body = (await client.get(_ROUTE, params={"scope": DESK})).json()
    assert body["version"] == COLLECTIONS_COVERAGE_VERSION
    assert body["scope"] == DESK
    holding = body["collections"][0]
    assert holding["collection_id"] == _COLLECTION
    assert holding["subject"] == "IR"
    assert holding["licence_class"] == "public"
    series = {s["series_id"]: s for s in holding["series"]}

    # COMPLETE — declared 2016-2018, held 2016-2018.
    assert series["wb.gdp"]["status"] == "held_complete"
    assert [(s["from_year"], s["to_year"]) for s in series["wb.gdp"]["held"]["spans"]] == [
        (2016, 2018)
    ]
    assert series["wb.gdp"]["holes"] == []

    # A LOAD GAP — declared three values, two on record, 2018 missing.
    assert series["wb.cpi"]["status"] == "held_with_holes"
    assert [(s["from_year"], s["to_year"]) for s in series["wb.cpi"]["holes"]] == [
        (2018, 2018)
    ]

    # THE PROVIDER'S OWN ABSENCE — never our gap, and it carries the
    # manifest's own reason rather than a restatement of it.
    assert series["wb.debt"]["status"] == "provider_holds_nothing"
    assert series["wb.debt"]["holes"] == []
    assert "Debtor Reporting System" in series["wb.debt"]["declared"]["reason"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_desk_no_holding_names_gets_the_routes_own_words(api_app, client):
    _, pg_store = api_app
    await _seed(pg_store)
    body = (await client.get(_ROUTE, params={"scope": UNKNOWN_DESK})).json()
    assert body["collections"] == []
    assert len(body["not_held"]) == 1
    # It names the desks the holdings DO cover — an empty map with no sentence
    # is a blank, which is the thing this route exists not to return.
    assert DESK in body["not_held"][0]
    assert BARE_DESK in body["not_held"][0]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_holding_awaiting_approval_is_invisible(api_app, client):
    """The operator's `draft` -> `reviewed` -> `loaded` approval is the gate.
    A `reviewed` holding has rows and must still read as no holding."""
    _, pg_store = api_app
    await _seed(pg_store, state="reviewed")
    body = (await client.get(_ROUTE, params={"scope": DESK})).json()
    assert body["collections"] == []
    assert "no collection is in state 'loaded'" in body["not_held"][0]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_map_carries_the_load_receipt_that_wrote_it(api_app, client):
    _, pg_store = api_app
    load_id = await _seed(pg_store)
    body = (await client.get(_ROUTE, params={"scope": DESK})).json()
    holding = body["collections"][0]
    assert holding["load_id"] == load_id
    assert holding["loaded_at"].startswith("2026-09-25T20:40:31")


# ---------------------------------------------------------------------------
# The typed absence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_history_gap_is_published_in_the_closed_vocabulary(client):
    body = (await client.get(_ABSENCE, params={"scope": DESK})).json()
    assert HISTORY_GAP_KIND in body["kinds"]
    assert "load" in body["kinds"][HISTORY_GAP_KIND]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_declared_but_unloaded_pair_is_one_history_gap(api_app, client):
    _, pg_store = api_app
    load_id = await _seed(pg_store)
    body = (await client.get(_ABSENCE, params={"scope": DESK})).json()
    gaps = [a for a in body["absences"] if a["kind"] == HISTORY_GAP_KIND]
    assert [g["subject"] for g in gaps] == ["wb.cpi:IR"]
    gap = gaps[0]
    # STAMPED BY THE LOAD, never by the read's own clock.
    assert gap["as_of"].startswith("2026-09-25T20:40:31")
    assert "completed load" in gap["as_of_basis"]
    expected = (_LOADED_AT + timedelta(days=GAP_SHELF_LIFE_DAYS)).isoformat()
    assert gap["expires_at"] == expected
    # A gap measured by a load that ran within the shelf life is CURRENT.
    assert gap["stale"] is False
    # The PROOF is the load that should have written the row — a gap is the
    # absence of a row, so the honest thing to point at is the run.
    assert gap["proof"]["ref"] == load_id
    assert gap["proof"]["ref_kind"] == "collection_load"
    assert "observations rows for series wb.cpi" in gap["proof"]["what_was_checked"]
    assert "missing valid years: 2018" == gap["window"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_gap_nobody_has_re_loaded_reads_LAST_KNOWN_not_re_checked(
    api_app, client
):
    """A collection has no cadence by construction, so the shelf life is a
    stated REVIEW interval rather than a schedule. Past it, the gap reads as
    last known — the same sentence every stale absence reads as, and for the
    same reason: an old absence must never pass for a current one."""
    _, pg_store = api_app
    old_load = datetime.now(timezone.utc) - timedelta(days=GAP_SHELF_LIFE_DAYS + 5)
    await _seed(pg_store, finished_at=old_load)
    body = (await client.get(_ABSENCE, params={"scope": DESK})).json()
    gap = [a for a in body["absences"] if a["kind"] == HISTORY_GAP_KIND][0]
    assert gap["stale"] is True


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_providers_own_absence_is_never_typed_as_our_gap(api_app, client):
    _, pg_store = api_app
    await _seed(pg_store)
    body = (await client.get(_ABSENCE, params={"scope": DESK})).json()
    subjects = {
        a["subject"] for a in body["absences"] if a["kind"] == HISTORY_GAP_KIND
    }
    assert "wb.debt:IR" not in subjects


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_desk_with_a_holding_and_no_rows_is_ONE_gap_for_the_desk(
    api_app, client
):
    """Not one per series: the load wrote nothing at all, and eleven items
    saying so is eleven copies of one fact."""
    _, pg_store = api_app
    await _seed(pg_store, with_rows=False)
    body = (await client.get(_ABSENCE, params={"scope": DESK})).json()
    gaps = [a for a in body["absences"] if a["kind"] == HISTORY_GAP_KIND]
    assert len(gaps) == 1
    assert gaps[0]["subject"] == f"{_COLLECTION}:IR"
    assert "holds no row for any of them" in gaps[0]["reason"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_desk_no_holding_names_is_NOT_MEASURED_not_a_gap(api_app, client):
    """A gap in a holding that does not exist is not an absence this platform
    can type — so it says which desks the holdings cover instead."""
    _, pg_store = api_app
    await _seed(pg_store)
    body = (await client.get(_ABSENCE, params={"scope": UNKNOWN_DESK})).json()
    assert not [a for a in body["absences"] if a["kind"] == HISTORY_GAP_KIND]
    said = [n for n in body["not_measured"] if DESK in n]
    assert said, body["not_measured"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_load_with_no_receipt_carries_no_clock_and_says_so(api_app, client):
    _, pg_store = api_app
    await _seed(pg_store, finished_at=None)
    body = (await client.get(_ABSENCE, params={"scope": DESK})).json()
    gap = [a for a in body["absences"] if a["kind"] == HISTORY_GAP_KIND][0]
    assert gap["as_of"] is None
    assert gap["expires_at"] is None
    assert gap["stale"] is False
    assert "no completed load" in gap["as_of_basis"]
    assert "load_collection.py" in gap["review"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_two_surfaces_read_the_SAME_measurement(api_app, client):
    """One reader, two surfaces. A second implementation over the same tables
    is how two surfaces start telling a reader two different stories about the
    same silence."""
    _, pg_store = api_app
    await _seed(pg_store)
    cov = (await client.get(_ROUTE, params={"scope": DESK})).json()
    absence = (await client.get(_ABSENCE, params={"scope": DESK})).json()
    gappy = {
        f"{s['series_id']}:{h['subject']}"
        for h in cov["collections"]
        for s in h["series"]
        if s["status"] in ("held_with_holes", "declared_not_loaded")
    }
    typed = {
        a["subject"] for a in absence["absences"] if a["kind"] == HISTORY_GAP_KIND
    }
    assert gappy == typed


# ---------------------------------------------------------------------------
# The slim-image guard
# ---------------------------------------------------------------------------


def test_the_coverage_route_imports_without_the_runtime_stack() -> None:
    """The registry ships a SLIM image; a route module that reaches
    ``legba.data.analysts`` or ``legba.runtime`` 500s live even when the import
    is deferred inside a function — deferring moves WHEN the graph is walked,
    never HOW FAR."""
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None

        import legba.data.registry.collections_api as api
        import legba.data.registry.collections_coverage as cov
        import legba.data.registry.absence_api as absence

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts")
               or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from the route: %r" % leaked
        assert api.build_collections_router is not None
        assert api.COLLECTIONS_COVERAGE_VERSION == "2026-09/7g-2"
        assert cov.HISTORY_GAP_KIND == "history_gap"
        assert "history_gap" in absence.ABSENCE_KINDS
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
        f"slim import failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "OK" in result.stdout
