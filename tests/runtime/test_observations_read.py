# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The COLLECTION series reads, on the REAL driver (7g-2 §6).

``observations`` is bitemporal and natively partitioned, and both properties
are things a hand-rolled stub cannot have. So every read here runs against the
migrated test database through ``asyncpg``, with rows that include the case the
whole design exists for: a 2016 figure the provider RESTATED in 2023, held as
two rows, where an as-of read before the restatement must return the FIRST
number and an as-of read after it must return the second.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.runtime import _observations_read as obs

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

#: Every row this file writes is tagged by collection id so cleanup is a
#: scoped DELETE, never a TRUNCATE of a session-shared table.
_TAG = f"t7g2read_{uuid4().hex[:8]}"
_COLLECTION = f"collection.{_TAG}"

_INSERT = """
    INSERT INTO observations
        (collection_id, series_id, subject_kind, subject, valid_from,
         valid_to, record_time, value, unit, source_url, sha256,
         origin_class, provenance)
    VALUES ($1, $2, 'country', $3, $4, $5, $6, $7, $8,
            'https://example.invalid/series.json', 'deadbeef', 'archive',
            $9::jsonb)
"""

_DESCRIPTOR = """
    INSERT INTO collection_descriptors
        (descriptor_id, version, schema_uri, abstraction_level, state, owner,
         name, body, is_head, origin_shape, origin_class, licence_class,
         collection_version)
    VALUES ($1, $2, 'legba/collection/1.0.0', 'L0', $3, 'test_7g2',
            'pilot under test', $4::jsonb, TRUE, 'archive_only', 'archive',
            'public', 'v1')
"""


@pytest_asyncio.fixture
async def pg_pool(migrated_pg):
    pool = await asyncpg.create_pool(
        host=migrated_pg.host,
        port=migrated_pg.port,
        user=migrated_pg.user,
        password=migrated_pg.password,
        database=migrated_pg.database,
        min_size=1,
        max_size=4,
    )
    yield pool
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM observations WHERE collection_id LIKE $1", f"collection.{_TAG}%"
        )
        await conn.execute(
            "DELETE FROM collection_descriptors WHERE descriptor_id LIKE $1",
            f"collection.{_TAG}%",
        )
    await pool.close()


async def _seed(conn, *, state: str = "loaded") -> None:
    body = (
        '{"subjects": [{"desk": "country_watch_%s", "subject": "IR", '
        '"name": "Iran"}, {"desk": "country_g20_us_%s", "subject": "US", '
        '"name": "United States"}], "subject_kind": "country"}'
    ) % (_TAG, _TAG)
    await conn.execute(_DESCRIPTOR, _COLLECTION, "0" * 64, state, body)
    prov = '{"provider": "world_bank", "indicator_name": "GDP growth (annual %)"}'
    # Ten years for IR, one series. 2016 carries TWO rows: the original 2017
    # publication and the 2023 restatement — the case an as-of read exists for.
    for year in range(2016, 2026):
        await conn.execute(
            _INSERT,
            _COLLECTION,
            "wb.gdp",
            "IR",
            date(year, 1, 1),
            date(year, 12, 31),
            datetime(year + 1, 7, 1, tzinfo=timezone.utc),
            Decimal(f"{year - 2010}.5"),
            "pct_per_year",
            prov,
        )
    await conn.execute(
        _INSERT,
        _COLLECTION,
        "wb.gdp",
        "IR",
        date(2016, 1, 1),
        date(2016, 12, 31),
        datetime(2023, 7, 1, tzinfo=timezone.utc),
        Decimal("99.9"),
        "pct_per_year",
        prov,
    )
    # A second subject and a second series, so the compare read and the
    # per-series read have something to distinguish.
    for year in (2016, 2017):
        await conn.execute(
            _INSERT,
            _COLLECTION,
            "wb.gdp",
            "US",
            date(year, 1, 1),
            date(year, 12, 31),
            datetime(year + 1, 7, 1, tzinfo=timezone.utc),
            Decimal("2.0"),
            "pct_per_year",
            prov,
        )
    await conn.execute(
        _INSERT,
        _COLLECTION,
        "wb.cpi",
        "IR",
        date(2025, 1, 1),
        date(2025, 12, 31),
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        Decimal("40.0"),
        "pct_per_year",
        prov,
    )


# ---------------------------------------------------------------------------
# series_history — the bitemporal rule
# ---------------------------------------------------------------------------


async def test_without_as_of_a_restated_year_reads_the_LATEST_revision(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn)
        rows = await obs.series_history(
            conn,
            collection_ids=[_COLLECTION],
            series_id="wb.gdp",
            subject="IR",
            valid_from=date(2016, 1, 1),
            valid_to=date(2025, 12, 31),
        )
    # ONE row per valid period, oldest period first — ten years, not eleven
    # rows, even though 2016 is stored twice.
    assert [r["valid_from"] for r in rows] == [
        f"{y}-01-01" for y in range(2016, 2026)
    ]
    assert rows[0]["value"] == pytest.approx(99.9)
    assert rows[0]["record_time"].startswith("2023-07-01")


async def test_as_of_before_the_restatement_reads_what_was_KNOWABLE_then(pg_pool):
    """THE CASE THE WHOLE TABLE IS BITEMPORAL FOR.

    A 2016 figure restated in 2023 is two rows. An as-of read at 2020 must
    return the number the provider had actually published by 2020 — not the
    one it would later publish. A single-time table cannot answer this, and no
    amount of reading recovers it afterwards.
    """
    async with pg_pool.acquire() as conn:
        await _seed(conn)
        rows = await obs.series_history(
            conn,
            collection_ids=[_COLLECTION],
            series_id="wb.gdp",
            subject="IR",
            valid_from=date(2016, 1, 1),
            valid_to=date(2016, 12, 31),
            as_of=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    assert len(rows) == 1
    assert rows[0]["value"] == pytest.approx(6.5)
    assert rows[0]["record_time"].startswith("2017-07-01")


async def test_as_of_before_anything_was_recorded_reads_NOTHING(pg_pool):
    """Not an error, and never the earliest row as a consolation prize: on
    that date the platform knew nothing about this period, and saying so is
    the answer."""
    async with pg_pool.acquire() as conn:
        await _seed(conn)
        rows = await obs.series_history(
            conn,
            collection_ids=[_COLLECTION],
            series_id="wb.gdp",
            subject="IR",
            valid_from=date(2016, 1, 1),
            valid_to=date(2025, 12, 31),
            as_of=datetime(2010, 1, 1, tzinfo=timezone.utc),
        )
    assert rows == []


async def test_the_window_bounds_the_read_at_both_ends(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn)
        rows = await obs.series_history(
            conn,
            collection_ids=[_COLLECTION],
            series_id="wb.gdp",
            subject="IR",
            valid_from=date(2018, 1, 1),
            valid_to=date(2020, 12, 31),
        )
    assert [r["valid_from"] for r in rows] == [
        "2018-01-01", "2019-01-01", "2020-01-01",
    ]


async def test_every_row_carries_its_own_provenance_and_a_citable_ref(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn)
        rows = await obs.series_history(
            conn,
            collection_ids=[_COLLECTION],
            series_id="wb.gdp",
            subject="IR",
            valid_from=date(2025, 1, 1),
            valid_to=date(2025, 12, 31),
        )
    row = rows[0]
    assert row["ref"] == f"observation:{row['observation_id']}"
    assert row["unit"] == "pct_per_year"
    assert row["source_url"] == "https://example.invalid/series.json"
    assert row["sha256"] == "deadbeef"
    assert row["origin_class"] == "archive"
    assert row["provider"] == "world_bank"
    # The number survives at the provider's own precision as well as a float:
    # float() of a long numeric loses digits a citation would then be wrong
    # about.
    assert row["value_display"] == "15.5"


async def test_an_unloaded_collection_is_not_readable(pg_pool):
    """The operator's approval is the gate. A holding still in `reviewed` has
    rows in the table and must not be readable through the front door."""
    async with pg_pool.acquire() as conn:
        await _seed(conn, state="reviewed")
        assert await obs.loaded_collections(conn) == [] or all(
            h["collection_id"] != _COLLECTION
            for h in await obs.loaded_collections(conn)
        )


# ---------------------------------------------------------------------------
# series_compare
# ---------------------------------------------------------------------------


async def test_compare_returns_every_named_subject_in_one_statement(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn)
        rows = await obs.series_compare(
            conn,
            collection_ids=[_COLLECTION],
            series_id="wb.gdp",
            subjects=["IR", "US"],
            valid_from=date(2016, 1, 1),
            valid_to=date(2017, 12, 31),
        )
    assert [(r["subject"], r["valid_from"]) for r in rows] == [
        ("IR", "2016-01-01"),
        ("IR", "2017-01-01"),
        ("US", "2016-01-01"),
        ("US", "2017-01-01"),
    ]
    # The restatement rule holds per (subject, period), not just per period.
    assert rows[0]["value"] == pytest.approx(99.9)


async def test_compare_honours_as_of_per_subject(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn)
        rows = await obs.series_compare(
            conn,
            collection_ids=[_COLLECTION],
            series_id="wb.gdp",
            subjects=["IR", "US"],
            valid_from=date(2016, 1, 1),
            valid_to=date(2016, 12, 31),
            as_of=datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
    assert {r["subject"]: r["value"] for r in rows} == {
        "IR": pytest.approx(6.5),
        "US": pytest.approx(2.0),
    }


async def test_a_subject_the_holding_lacks_is_absent_never_zero(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn)
        rows = await obs.series_compare(
            conn,
            collection_ids=[_COLLECTION],
            series_id="wb.gdp",
            subjects=["IR", "ZZ"],
            valid_from=date(2016, 1, 1),
            valid_to=date(2016, 12, 31),
        )
    assert [r["subject"] for r in rows] == ["IR"]


# ---------------------------------------------------------------------------
# latest_per_series + the desk resolution
# ---------------------------------------------------------------------------


async def test_latest_per_series_reads_one_line_per_series(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn)
        rows = await obs.latest_per_series(
            conn,
            collection_ids=[_COLLECTION],
            subject="IR",
            valid_from=date(2016, 1, 1),
        )
    assert [(r["series_id"], r["valid_from"]) for r in rows] == [
        ("wb.cpi", "2025-01-01"),
        ("wb.gdp", "2025-01-01"),
    ]


async def test_a_desk_resolves_to_its_subject_off_the_HOLDINGS_own_block(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn)
        pairs = await obs.desk_subjects(conn, desk=f"country_watch_{_TAG}")
    assert [(p["collection_id"], p["subject"]) for p in pairs] == [
        (_COLLECTION, "IR")
    ]


async def test_a_desk_no_loaded_holding_names_resolves_to_nothing(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn)
        assert await obs.desk_subjects(conn, desk="country_watch_nowhere") == []


# ---------------------------------------------------------------------------
# The tool surface — refusals, not silent widenings
# ---------------------------------------------------------------------------


async def test_a_missing_window_REFUSES_rather_than_reading_all_time(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn)
    out = await obs.read_series_history(
        pg_pool, series_id="wb.gdp", subject="IR", since=None, until="2025"
    )
    assert out["rows"] == []
    assert "never all-time" in out["error"]


async def test_a_malformed_bound_REFUSES_with_its_own_name(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn)
    out = await obs.read_series_history(
        pg_pool, series_id="wb.gdp", subject="IR", since="last tuesday", until="2025"
    )
    assert "not a date" in out["error"]


async def test_a_bare_year_bound_is_that_years_edge(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn)
    out = await obs.read_series_history(
        pg_pool, series_id="wb.gdp", subject="IR", since="2018", until="2019"
    )
    assert out["valid_from"] == "2018-01-01"
    assert out["valid_to"] == "2019-12-31"
    assert out["count"] == 2
    assert out["refs"] == [r["observation_id"] for r in out["rows"]]


async def test_asking_for_an_unloaded_collection_REFUSES_by_name(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn)
    out = await obs.read_series_history(
        pg_pool,
        series_id="wb.gdp",
        subject="IR",
        since="2016",
        until="2025",
        collection_id="collection.not_loaded",
    )
    assert "is not loaded" in out["error"]


async def test_the_tool_answer_names_the_subjects_that_returned_nothing(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn)
    out = await obs.read_series_compare(
        pg_pool,
        series_id="wb.gdp",
        subjects=["IR", "ZZ"],
        since="2016",
        until="2016",
    )
    assert out["subjects_with_no_rows"] == ["ZZ"]
    assert out["as_of"] is None
    assert "latest revision on record" in out["as_of_note"]


async def test_the_tool_answer_states_the_as_of_rule_it_applied(pg_pool):
    async with pg_pool.acquire() as conn:
        await _seed(conn)
    out = await obs.read_series_history(
        pg_pool,
        series_id="wb.gdp",
        subject="IR",
        since="2016",
        until="2016",
        as_of="2020-01-01T00:00:00Z",
    )
    assert out["count"] == 1
    assert out["rows"][0]["value"] == pytest.approx(6.5)
    assert "RECORDED ON OR BEFORE" in out["as_of_note"]


# ---------------------------------------------------------------------------
# The census read
# ---------------------------------------------------------------------------


async def test_classify_cited_refs_reads_the_rows_OWN_origin_class(pg_pool):
    from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort

    async with pg_pool.acquire() as conn:
        await _seed(conn)
        ids = [
            str(r["id"])
            for r in await conn.fetch(
                "SELECT id FROM observations WHERE collection_id = $1 LIMIT 3",
                _COLLECTION,
            )
        ]
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pg_pool, qdrant_client=None)
    out = await port.classify_cited_refs(refs=ids + ["not-a-uuid", str(uuid4())])
    assert out["by_origin_class"] == {"archive": 3}
    assert out["resolved"] == 3
    # The unknown uuid is UNRESOLVED, never folded into a class it might not
    # belong to — a census that absorbs what it cannot resolve overstates
    # whichever bucket took it.
    assert out["unresolved"] == 1
