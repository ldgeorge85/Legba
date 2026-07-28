# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the grown flavor-b seed adapters: wikidata_leaders + acled_conflict.

Covers (planning/SEEDING_SKETCH.md "grow (b)"):

  * both adapters are registered in ``ADAPTERS`` with the right source_type;
  * the Wikidata SPARQL adapter maps SPARQL JSON bindings → LeaderOf facts +
    signed MemberOf nexuses, with real valid_from, skipping undated/unlabelled;
  * the ACLED adapter maps conflict records → InvolvedInConflictEvent facts +
    signed -1 HostileTo nexuses, geo-stamped, skipping records w/o event_date;
  * end-to-end through the driver: fetched-from-fixture → resolved → written
    idempotently with the batch marker (no dup open triples on re-run).
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.seed import (
    SeedContext,
    SeedFact,
    SeedNexus,
    get_adapter,
    list_adapters,
    run_seed_source,
)
from legba.data.seed.adapters.acled_conflict import ACLEDConflictSeedSource
from legba.data.seed.adapters.wikidata_leaders import (
    WikidataLeadersSeedSource,
    _parse_wikidata_time,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


def _wd_binding(value: str) -> dict[str, str]:
    return {"value": value}


_WD_FIXTURE = {
    "leaders": [
        {
            "country": _wd_binding("http://www.wikidata.org/entity/Q142"),
            "countryLabel": _wd_binding("France"),
            "leader": _wd_binding("http://www.wikidata.org/entity/Q3052772"),
            "leaderLabel": _wd_binding("Emmanuel Macron"),
            "role": _wd_binding("head_of_state"),
            "start": _wd_binding("+2017-05-14T00:00:00Z"),
        },
        {
            # no term start → must be skipped (no fabricated valid_from)
            "country": _wd_binding("http://www.wikidata.org/entity/Q183"),
            "countryLabel": _wd_binding("Germany"),
            "leader": _wd_binding("http://www.wikidata.org/entity/Q568"),
            "leaderLabel": _wd_binding("Some Leader"),
            "role": _wd_binding("head_of_government"),
        },
        {
            # year-precision start (month/day are 00) → repaired to 01-01
            "country": _wd_binding("http://www.wikidata.org/entity/Q668"),
            "countryLabel": _wd_binding("India"),
            "leader": _wd_binding("http://www.wikidata.org/entity/Q1058"),
            "leaderLabel": _wd_binding("Narendra Modi"),
            "role": _wd_binding("head_of_government"),
            "start": _wd_binding("+2014-00-00T00:00:00Z"),
        },
    ],
    "alliances": [
        {
            "country": _wd_binding("http://www.wikidata.org/entity/Q142"),
            "countryLabel": _wd_binding("France"),
            "bloc": _wd_binding("http://www.wikidata.org/entity/Q7184"),
            "blocLabel": _wd_binding("NATO"),
            "start": _wd_binding("+1949-04-04T00:00:00Z"),
        },
    ],
}


_ACLED_FIXTURE = [
    {
        "data_id": "1001",
        "event_date": "2024-03-15",
        "event_type": "Battles",
        "sub_event_type": "Armed clash",
        "actor1": "Military Forces of Country X",
        "actor2": "Rebel Group Y",
        "country": "Country X",
        "iso3": "CXX",
        "latitude": "12.34",
        "longitude": "56.78",
        "fatalities": 5,
        "location": "Town A",
    },
    {
        # no event_date → skipped (no fabricated valid_from)
        "data_id": "1002",
        "event_date": "",
        "actor1": "Some Actor",
        "actor2": "Another Actor",
        "country": "Country Z",
    },
    {
        # single actor (no actor2) → fact only, no HostileTo nexus
        "data_id": "1003",
        "event_date": "2024-04-01",
        "event_type": "Protests",
        "actor1": "Protesters (Country X)",
        "actor2": "",
        "country": "Country X",
        "latitude": "10.0",
        "longitude": "20.0",
    },
]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_growth_adapters_registered():
    names = dict(list_adapters())
    assert names.get("wikidata_leaders") == "seed"
    assert names.get("acled_conflict") == "backfill"
    assert get_adapter("wikidata_leaders").name == "wikidata_leaders"
    assert get_adapter("acled_conflict").name == "acled_conflict"


# ---------------------------------------------------------------------------
# Wikidata SPARQL adapter — mapping (no network)
# ---------------------------------------------------------------------------


def test_parse_wikidata_time_repairs_and_signs():
    assert _parse_wikidata_time("+2017-05-14T00:00:00Z").year == 2017
    # year-precision sentinel month/day 00 repaired to Jan 1
    dt = _parse_wikidata_time("+2014-00-00T00:00:00Z")
    assert (dt.year, dt.month, dt.day) == (2014, 1, 1)
    assert _parse_wikidata_time(None) is None
    assert _parse_wikidata_time("garbage") is None


@pytest.mark.asyncio
async def test_wikidata_maps_facts_and_signed_nexuses():
    adapter = WikidataLeadersSeedSource()
    raw = await adapter.fetch(SeedContext(options={"sparql_json": _WD_FIXTURE}))
    payloads = list(adapter.map(raw))

    facts = [p for p in payloads if isinstance(p, SeedFact)]
    nexuses = [p for p in payloads if isinstance(p, SeedNexus)]

    # Each dated leader yields a subject=leader `LeaderOf` fact AND a
    # subject=country office fact, TYPED by office (DQ-#85.3): a P35 holder →
    # `head of state`, a P6 holder → `head of government` (separate keys).
    leader_of = [f for f in facts if f.predicate == "LeaderOf"]
    office_state = [f for f in facts if f.predicate == "head of state"]
    office_gov = [f for f in facts if f.predicate == "head of government"]
    # Macron + Modi map; the undated German leader is skipped.
    leader_subjects = {f.subject for f in leader_of}
    assert "Emmanuel Macron" in leader_subjects
    assert "Narendra Modi" in leader_subjects
    assert "Some Leader" not in leader_subjects, "undated leader must be skipped"
    # France's Macron is a head of state (P35); India's Modi a head of
    # government (P6) — the two land on DISTINCT predicates, never collapsed.
    assert {f.subject for f in office_state} == {"France"}
    assert office_state[0].value == "Emmanuel Macron"
    assert {f.subject for f in office_gov} == {"India"}
    assert office_gov[0].value == "Narendra Modi"
    # One office fact per dated leader, across both office predicates.
    assert len(office_state) + len(office_gov) == len(leader_of)
    for f in facts:
        assert f.predicate in ("LeaderOf", "head of state", "head of government")
        assert isinstance(f.valid_from, datetime)

    # The alliance maps to a +1 signed MemberOf nexus.
    assert len(nexuses) == 1
    nx = nexuses[0]
    assert nx.subject == "France" and nx.object == "NATO"
    assert nx.rel_type == "MemberOf" and nx.polarity == 1
    assert isinstance(nx.valid_from, datetime)


# ---------------------------------------------------------------------------
# S-3: current-holder selection (stale ex-leaders must NOT seed)
# ---------------------------------------------------------------------------


def _drc_gov_row(leader: str, start: str, end: str | None = None) -> dict:
    """A DR Congo head-of-government (P6) SPARQL binding fixture row."""
    row = {
        "country": _wd_binding("http://www.wikidata.org/entity/Q974"),
        "countryLabel": _wd_binding("Democratic Republic of the Congo"),
        "leader": _wd_binding(f"http://www.wikidata.org/entity/{leader.replace(' ', '_')}"),
        "leaderLabel": _wd_binding(leader),
        "role": _wd_binding("head_of_government"),
        "start": _wd_binding(start),
    }
    if end is not None:
        row["end"] = _wd_binding(end)
    return row


@pytest.mark.asyncio
async def test_wikidata_current_holder_wins_over_former_with_end_date():
    # DR Congo head of government (S-3): a FORMER PM carrying an end-date +
    # the incumbent (no end-date). The adapter must emit ONLY the incumbent —
    # for BOTH the country-subject office fact AND the subject=leader LeaderOf
    # fact, so the ex-PM never enters `facts` at all.
    fixture = {
        "leaders": [
            _drc_gov_row("Sylvestre Ilunga Ilunkamba", "+2019-05-20T00:00:00Z",
                         end="+2021-04-12T00:00:00Z"),
            _drc_gov_row("Judith Suminwa Tuluka", "+2024-06-12T00:00:00Z"),
        ],
        "alliances": [],
    }
    adapter = WikidataLeadersSeedSource()
    raw = await adapter.fetch(SeedContext(options={"sparql_json": fixture}))
    facts = [p for p in adapter.map(raw) if isinstance(p, SeedFact)]

    gov = [f for f in facts if f.predicate == "head of government"]
    assert len(gov) == 1, "exactly one current head of government per country"
    assert gov[0].subject == "Democratic Republic of the Congo"
    assert gov[0].value == "Judith Suminwa Tuluka"

    leader_of = {f.subject for f in facts if f.predicate == "LeaderOf"}
    assert leader_of == {"Judith Suminwa Tuluka"}
    assert "Sylvestre Ilunga Ilunkamba" not in leader_of, "stale ex-PM must not seed"


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_wikidata_multiple_no_end_holders_latest_start_wins(reverse: bool):
    # Wikidata DATA LAG: TWO head-of-government statements for one country BOTH
    # lack an end-date (no preferred rank to let BestRank pick the incumbent).
    # The most-recent term-start (P580) must win — regardless of row order.
    rows = [
        _drc_gov_row("Former Holder", "+2019-05-20T00:00:00Z"),   # older start
        _drc_gov_row("Current Holder", "+2024-06-12T00:00:00Z"),  # later start
    ]
    if reverse:
        rows.reverse()
    adapter = WikidataLeadersSeedSource()
    raw = await adapter.fetch(SeedContext(options={"sparql_json": {"leaders": rows, "alliances": []}}))
    facts = [p for p in adapter.map(raw) if isinstance(p, SeedFact)]

    gov = [f for f in facts if f.predicate == "head of government"]
    assert len(gov) == 1
    assert gov[0].value == "Current Holder"
    assert {f.subject for f in facts if f.predicate == "LeaderOf"} == {"Current Holder"}


@pytest.mark.asyncio
async def test_wikidata_leader_facts_carry_as_of_seed_stamp():
    # Every leader fact records the seed instant in data['as_of'] so a downstream
    # freshness pass can age the (flat 0.92) confidence.
    adapter = WikidataLeadersSeedSource()
    raw = await adapter.fetch(SeedContext(options={"sparql_json": _WD_FIXTURE}))
    facts = [p for p in adapter.map(raw) if isinstance(p, SeedFact)]
    assert facts
    for f in facts:
        assert "as_of" in f.data, f"{f.predicate} fact missing as_of stamp"
        # a parseable ISO instant
        datetime.fromisoformat(str(f.data["as_of"]))


@pytest.mark.asyncio
async def test_wikidata_current_holder_still_degrades_undated_and_unlabelled():
    # The degrade paths survive the current-holder selection: an undated holder
    # is still skipped, a bare/unlabelled one still dropped, and a normal one for
    # a DIFFERENT country still maps.
    fixture = {
        "leaders": [
            _drc_gov_row("Judith Suminwa Tuluka", "+2024-06-12T00:00:00Z"),
            {  # undated → skipped (no fabricated valid_from)
                "country": _wd_binding("http://www.wikidata.org/entity/Q183"),
                "countryLabel": _wd_binding("Germany"),
                "leader": _wd_binding("http://www.wikidata.org/entity/Q568"),
                "leaderLabel": _wd_binding("Undated Person"),
                "role": _wd_binding("head_of_government"),
            },
            {  # bare-QID leaderLabel with no resolution → dropped
                "country": _wd_binding("http://www.wikidata.org/entity/Q155"),
                "countryLabel": _wd_binding("Brazil"),
                "leader": _wd_binding("http://www.wikidata.org/entity/Q7654321"),
                "leaderLabel": _wd_binding("Q7654321"),
                "role": _wd_binding("head_of_government"),
                "start": _wd_binding("+2023-01-01T00:00:00Z"),
            },
        ],
        "alliances": [],
    }
    adapter = WikidataLeadersSeedSource()
    # No label API resolution set → the bare QID stays bare → dropped by map().
    adapter._sparql_client_factory = lambda: _NullLabelClient()
    raw = await adapter.fetch(SeedContext(options={"sparql_json": fixture}))
    facts = [p for p in adapter.map(raw) if isinstance(p, SeedFact)]

    gov_subjects = {f.subject for f in facts if f.predicate == "head of government"}
    assert gov_subjects == {"Democratic Republic of the Congo"}
    values = {f.value for f in facts} | {f.subject for f in facts}
    assert "Undated Person" not in values
    assert not any(v.startswith("Q") and v[1:].isdigit() for v in values)


class _NullLabelClient:
    """A label-API client that resolves nothing (entities empty)."""

    async def __aenter__(self) -> "_NullLabelClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, *a: object, **k: object):
        class _R:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"entities": {}}

        return _R()


# ---------------------------------------------------------------------------
# ACLED adapter — mapping (no network)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acled_maps_events_and_hostile_nexuses():
    adapter = ACLEDConflictSeedSource()
    raw = await adapter.fetch(SeedContext(options={"records": _ACLED_FIXTURE}))
    payloads = list(adapter.map(raw))

    facts = [p for p in payloads if isinstance(p, SeedFact)]
    nexuses = [p for p in payloads if isinstance(p, SeedNexus)]

    # Two parseable events → two conflict-event facts; the dateless one skipped.
    assert len(facts) == 2
    for f in facts:
        assert f.predicate == "InvolvedInConflictEvent"
        assert isinstance(f.valid_from, datetime)
    # Geo carried through from the event coords.
    battle_fact = next(f for f in facts if f.subject == "Military Forces of Country X")
    assert battle_fact.geo_lat == pytest.approx(12.34)

    # Only the two-actor event yields a signed -1 HostileTo nexus.
    assert len(nexuses) == 1
    nx = nexuses[0]
    assert nx.rel_type == "HostileTo" and nx.polarity == -1
    assert nx.subject == "Military Forces of Country X"
    assert nx.object == "Rebel Group Y"


@pytest.mark.asyncio
async def test_acled_requires_credentials_without_fixture():
    adapter = ACLEDConflictSeedSource()
    with pytest.raises(ValueError, match="api_key"):
        await adapter.fetch(SeedContext(options={}))


# ---------------------------------------------------------------------------
# End-to-end through the driver + idempotency
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wikidata_driver_end_to_end_and_idempotent(pg_pool):
    adapter = WikidataLeadersSeedSource()
    opts = {"sparql_json": _WD_FIXTURE}

    r1 = await run_seed_source(pg_pool, adapter, dry_run=False, options=opts)
    assert not r1.errors, f"unexpected errors: {r1.errors}"
    assert r1.seed_batch_id is not None
    assert r1.counts["facts"] >= 2
    assert r1.counts["nexuses"] >= 1

    async with pg_pool.acquire() as conn:
        macron = await conn.fetchrow(
            "SELECT source_type, seed_batch_id, valid_from FROM facts "
            # Phase B item 5: predicate stored canonical (was 'LeaderOf').
            "WHERE lower(subject)='emmanuel macron' AND predicate='leader of' "
            "AND lower(value)='france' AND valid_until IS NULL AND superseded_by IS NULL"
        )
        assert macron is not None
        assert macron["source_type"] == "seed"
        # The row is batch-stamped. (Exact batch == r1 only on a FRESH write; if
        # the session DB already carries this open triple from world_baseline,
        # the upsert is a no-op that intentionally keeps the FIRST batch stamp —
        # idempotency. So we assert it is stamped, not which batch.)
        assert macron["seed_batch_id"] is not None

        nato = await conn.fetchrow(
            "SELECT polarity, source_type FROM nexuses "
            # Phase B item 5: rel_type stored canonical (was 'MemberOf').
            "WHERE lower(subject)='france' AND rel_type='member of' "
            "AND lower(object)='nato' AND valid_until IS NULL AND superseded_by IS NULL"
        )
        assert nato is not None and nato["polarity"] == 1

        open_facts_1 = await conn.fetchval(
            "SELECT count(*) FROM facts WHERE source_type='seed' "
            "AND valid_until IS NULL AND superseded_by IS NULL"
        )

    # Re-run is idempotent — no new open fact rows.
    r2 = await run_seed_source(pg_pool, adapter, dry_run=False, options=opts)
    assert not r2.errors
    async with pg_pool.acquire() as conn:
        open_facts_2 = await conn.fetchval(
            "SELECT count(*) FROM facts WHERE source_type='seed' "
            "AND valid_until IS NULL AND superseded_by IS NULL"
        )
    assert open_facts_2 == open_facts_1, "re-run must not add open fact rows"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_acled_driver_end_to_end_backfill(pg_pool):
    adapter = ACLEDConflictSeedSource()
    opts = {"records": _ACLED_FIXTURE}

    r1 = await run_seed_source(pg_pool, adapter, dry_run=False, options=opts)
    assert not r1.errors, f"unexpected errors: {r1.errors}"
    assert r1.source_type == "backfill"

    async with pg_pool.acquire() as conn:
        batch = await conn.fetchrow(
            "SELECT source_type FROM seed_batches WHERE id=$1", r1.seed_batch_id
        )
        assert batch["source_type"] == "backfill"

        hostile = await conn.fetchrow(
            "SELECT polarity, source_type FROM nexuses "
            # Phase B item 5: rel_type stored canonical (was 'HostileTo').
            "WHERE rel_type='hostile to' AND source_type='backfill' "
            "AND valid_until IS NULL AND superseded_by IS NULL LIMIT 1"
        )
        assert hostile is not None
        assert hostile["polarity"] == -1
