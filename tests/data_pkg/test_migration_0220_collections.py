# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0220 — the collections core, against real SQL.

Every property asserted here is a property of the DATABASE, not of the loader
that sits on top of it. The loader is a program that can be rewritten; the
table is the thing that has to keep a ten-year holding honest no matter what
writes to it, so each of these is checked against a bare INSERT that never
goes near ``scripts/load_collection.py``:

* ``observations`` is natively partitioned by ``valid_from`` and a row LANDS
  in its year's partition (the §D.4 decision, with no TimescaleDB anywhere);
* the identity unique key refuses a duplicate — which is what makes the
  loader idempotent, rather than the loader's own bookkeeping;
* a 2016 figure revised twice is TWO rows, because ``record_time`` is part of
  that key — the bitemporal property the whole table exists for;
* the origin-class CHECK refuses ``live``/``web_retrieval``/``seed``: a
  collection can never write a live-class row;
* a row with neither ``value`` nor ``value_text`` — and a row with both — is
  refused, so "absence" can never be stored as a half-row;
* the SEAMS #57 sweep landed exactly as the migration's header says: all
  three seam constraints are RENAMED, not dropped — the readers are swept
  (#57 resolved) but no history WRITER exists for those tables yet (#62), and
  the facts guard has a concrete unsolved reason of its own.

A second apply of the migration is a no-op.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.migrations import MIGRATIONS_DIR

MIGRATION_NAME = "0220_collections.sql"
_TAG = "t0220"


def _migration_sql() -> str:
    return (MIGRATIONS_DIR / MIGRATION_NAME).read_text(encoding="utf-8")


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    yield c
    await c.execute(
        "DELETE FROM observations WHERE collection_id LIKE $1", f"{_TAG}%"
    )
    await c.execute(
        "DELETE FROM collection_loads WHERE collection_id LIKE $1", f"{_TAG}%"
    )
    await c.execute(
        "DELETE FROM entity_aliases WHERE collection_id LIKE $1", f"{_TAG}%"
    )
    await c.close()


_INSERT = """
    INSERT INTO observations
        (collection_id, series_id, subject_kind, subject, valid_from,
         valid_to, record_time, value, unit, source_url, sha256, origin_class)
    VALUES ($1, $2, 'country', $3, $4, $5, $6, $7, 'pct_per_year',
            'https://example.invalid/x', 'deadbeef', $8)
"""


async def _insert(
    conn,
    *,
    collection_id: str,
    series_id: str = "s.one",
    subject: str = "IR",
    year: int = 2016,
    record_time: datetime | None = None,
    value: str = "1.5",
    origin_class: str = "archive",
) -> None:
    await conn.execute(
        _INSERT,
        collection_id,
        series_id,
        subject,
        date(year, 1, 1),
        date(year, 12, 31),
        record_time or datetime(2026, 7, 13, tzinfo=timezone.utc),
        Decimal(value),
        origin_class,
    )


# ---------------------------------------------------------------------------
# The four tables exist with the shape the design note names
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observations_has_the_columns_section_three_names(conn) -> None:
    rows = await conn.fetch(
        """
        SELECT column_name, data_type, is_nullable
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'observations'
        """
    )
    by_name = {r["column_name"]: r for r in rows}
    expected = {
        "id", "collection_id", "series_id", "subject_kind", "subject",
        "valid_from", "valid_to", "record_time", "loaded_at", "value",
        "unit", "value_text", "source_url", "sha256", "origin_class",
        "provenance",
    }
    assert expected <= set(by_name), f"missing: {expected - set(by_name)}"
    assert by_name["valid_from"]["data_type"] == "date"
    assert by_name["valid_to"]["data_type"] == "date"
    assert by_name["record_time"]["data_type"] == "timestamp with time zone"
    assert by_name["value"]["data_type"] == "numeric"
    assert by_name["provenance"]["data_type"] == "jsonb"
    for required in ("collection_id", "series_id", "subject", "valid_from",
                     "valid_to", "record_time", "unit", "source_url",
                     "sha256", "origin_class"):
        assert by_name[required]["is_nullable"] == "NO", required
    # Absence is a MISSING ROW, never a null value in a present one.
    assert by_name["value"]["is_nullable"] == "YES"
    assert by_name["value_text"]["is_nullable"] == "YES"


@pytest.mark.asyncio
async def test_the_other_three_tables_exist(conn) -> None:
    for table in ("collection_descriptors", "entity_aliases",
                  "collection_loads"):
        assert await conn.fetchval(
            "SELECT to_regclass($1)", f"public.{table}"
        ) is not None, table


@pytest.mark.asyncio
async def test_observations_is_natively_partitioned_by_valid_from(conn) -> None:
    strategy = await conn.fetchval(
        """
        SELECT partstrat FROM pg_partitioned_table
         WHERE partrelid = 'public.observations'::regclass
        """
    )
    assert strategy in ("r", b"r"), "observations must be RANGE-partitioned"
    key = await conn.fetchval(
        """
        SELECT pg_get_partkeydef('public.observations'::regclass)
        """
    )
    assert "valid_from" in key
    names = {
        r["relname"]
        for r in await conn.fetch(
            """
            SELECT c.relname
              FROM pg_inherits i
              JOIN pg_class c ON c.oid = i.inhrelid
             WHERE i.inhparent = 'public.observations'::regclass
            """
        )
    }
    for year in range(2016, 2028):
        assert f"observations_{year}" in names
    assert "observations_default" in names


@pytest.mark.asyncio
async def test_a_row_lands_in_its_year_partition(conn) -> None:
    cid = f"{_TAG}_route"
    await _insert(conn, collection_id=cid, year=2016)
    await _insert(conn, collection_id=cid, year=2025, series_id="s.two")
    # Out of the declared range on purpose — the DEFAULT partition catches it
    # rather than the insert failing.
    await _insert(conn, collection_id=cid, year=1999, series_id="s.three")
    routed = {
        (r["tbl"], r["valid_from"].year)
        for r in await conn.fetch(
            "SELECT tableoid::regclass::text AS tbl, valid_from "
            "FROM observations WHERE collection_id = $1",
            cid,
        )
    }
    assert ("observations_2016", 2016) in routed
    assert ("observations_2025", 2025) in routed
    assert ("observations_default", 1999) in routed


# ---------------------------------------------------------------------------
# The identity key — what makes the loader idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_identity_key_refuses_a_duplicate(conn) -> None:
    cid = f"{_TAG}_dupe"
    await _insert(conn, collection_id=cid)
    with pytest.raises(asyncpg.exceptions.UniqueViolationError) as exc:
        await _insert(conn, collection_id=cid)
    # A partitioned unique index reports under the PARTITION's local index
    # name, so the assertion is on the key it names, not on the parent's.
    message = str(exc.value)
    assert "observations_2016" in message
    assert (
        "(collection_id, series_id, subject, valid_from, valid_to, "
        "record_time)"
    ) in message


@pytest.mark.asyncio
async def test_a_revision_is_a_second_row_not_an_overwrite(conn) -> None:
    """The bitemporal property: a 2016 figure revised in 2023 is TWO rows.

    ``record_time`` is part of the identity key precisely so a provider that
    restates history cannot silently replace what was knowable before.
    """
    cid = f"{_TAG}_bitemporal"
    await _insert(
        conn, collection_id=cid, year=2016, value="1.1",
        record_time=datetime(2017, 4, 1, tzinfo=timezone.utc),
    )
    await _insert(
        conn, collection_id=cid, year=2016, value="1.4",
        record_time=datetime(2023, 7, 13, tzinfo=timezone.utc),
    )
    rows = await conn.fetch(
        "SELECT value, record_time FROM observations "
        "WHERE collection_id = $1 ORDER BY record_time",
        cid,
    )
    assert len(rows) == 2
    assert [float(r["value"]) for r in rows] == [1.1, 1.4]
    # An as-of read at 2020 sees only the first.
    asof = await conn.fetch(
        "SELECT DISTINCT ON (series_id, valid_from, valid_to) value "
        "  FROM observations "
        " WHERE collection_id = $1 AND record_time <= $2 "
        " ORDER BY series_id, valid_from, valid_to, record_time DESC",
        cid, datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    assert [float(r["value"]) for r in asof] == [1.1]


# ---------------------------------------------------------------------------
# The CHECKs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("live", ["live", "web_retrieval", "seed"])
@pytest.mark.asyncio
async def test_a_live_origin_class_is_refused_at_the_table(conn, live) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc:
        await _insert(
            conn, collection_id=f"{_TAG}_cls", origin_class=live,
        )
    assert "observations_origin_class_history_only" in str(exc.value)


@pytest.mark.asyncio
async def test_a_row_with_no_value_at_all_is_refused(conn) -> None:
    """Absence is a MISSING ROW. A row that carries neither a number nor a
    text value is not an observation, and the table says so."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc:
        await conn.execute(
            """
            INSERT INTO observations
                (collection_id, series_id, subject_kind, subject, valid_from,
                 valid_to, record_time, unit, source_url, sha256, origin_class)
            VALUES ($1, 's.one', 'country', 'IR', '2016-01-01', '2016-12-31',
                    now(), 'pct_per_year', 'u', 'd', 'archive')
            """,
            f"{_TAG}_novalue",
        )
    assert "observations_exactly_one_value" in str(exc.value)


@pytest.mark.asyncio
async def test_a_row_with_both_value_kinds_is_refused(conn) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc:
        await conn.execute(
            """
            INSERT INTO observations
                (collection_id, series_id, subject_kind, subject, valid_from,
                 valid_to, record_time, value, value_text, unit, source_url,
                 sha256, origin_class)
            VALUES ($1, 's.one', 'country', 'IR', '2016-01-01', '2016-12-31',
                    now(), 1.0, 'one', 'pct_per_year', 'u', 'd', 'archive')
            """,
            f"{_TAG}_bothvalues",
        )
    assert "observations_exactly_one_value" in str(exc.value)


@pytest.mark.asyncio
async def test_an_inverted_period_is_refused(conn) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc:
        await conn.execute(
            _INSERT,
            f"{_TAG}_period", "s.one", "IR",
            date(2020, 12, 31), date(2020, 1, 1),
            datetime(2026, 1, 1, tzinfo=timezone.utc), Decimal("1.0"),
            "archive",
        )
    assert "observations_period_ordered" in str(exc.value)


@pytest.mark.asyncio
async def test_an_unknown_subject_kind_is_refused(conn) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(
            """
            INSERT INTO observations
                (collection_id, series_id, subject_kind, subject, valid_from,
                 valid_to, record_time, value, unit, source_url, sha256,
                 origin_class)
            VALUES ($1, 's.one', 'planet', 'IR', '2016-01-01', '2016-12-31',
                    now(), 1.0, 'u', 'u', 'd', 'archive')
            """,
            f"{_TAG}_kind",
        )


# ---------------------------------------------------------------------------
# collection_loads + entity_aliases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collection_loads_is_one_row_per_collection_version(conn) -> None:
    cid = f"{_TAG}_ledger"
    await conn.execute(
        "INSERT INTO collection_loads (collection_id, collection_version, "
        "loader_kind) VALUES ($1, 'v1', 'series_api')", cid,
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError) as exc:
        await conn.execute(
            "INSERT INTO collection_loads (collection_id, collection_version, "
            "loader_kind) VALUES ($1, 'v1', 'series_api')", cid,
        )
    assert "collection_loads_version_unique" in str(exc.value)
    # A different manifest version is a different load.
    await conn.execute(
        "INSERT INTO collection_loads (collection_id, collection_version, "
        "loader_kind) VALUES ($1, 'v2', 'series_api')", cid,
    )
    assert await conn.fetchval(
        "SELECT count(*) FROM collection_loads WHERE collection_id = $1", cid
    ) == 2


@pytest.mark.asyncio
async def test_collection_loads_status_vocabulary_is_closed(conn) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc:
        await conn.execute(
            "INSERT INTO collection_loads (collection_id, collection_version, "
            "loader_kind, status) VALUES ($1, 'v1', 'series_api', 'probably')",
            f"{_TAG}_status",
        )
    assert "collection_loads_status_vocab" in str(exc.value)


@pytest.mark.asyncio
async def test_entity_aliases_is_one_entity_per_alias_per_collection(conn) -> None:
    cid = f"{_TAG}_alias"
    entity = uuid4()
    await conn.execute(
        "INSERT INTO entity_aliases (alias, entity_id, collection_id, source) "
        "VALUES ('Iran, Islamic Rep.', $1, $2, 'world_bank')", entity, cid,
    )
    with pytest.raises(asyncpg.exceptions.UniqueViolationError):
        await conn.execute(
            "INSERT INTO entity_aliases (alias, entity_id, collection_id, "
            "source) VALUES ('Iran, Islamic Rep.', $1, $2, 'eia')",
            uuid4(), cid,
        )
    # …but the same surface form in ANOTHER collection is its own mapping.
    await conn.execute(
        "INSERT INTO entity_aliases (alias, entity_id, collection_id, source) "
        "VALUES ('Iran, Islamic Rep.', $1, $2, 'eia')",
        uuid4(), f"{cid}_other",
    )
    await conn.execute(
        "DELETE FROM entity_aliases WHERE collection_id = $1", f"{cid}_other"
    )


# ---------------------------------------------------------------------------
# The SEAMS #57 sweep, as the header describes it
# ---------------------------------------------------------------------------


async def _constraint_names(conn, table: str) -> set[str]:
    return {
        r["conname"]
        for r in await conn.fetch(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = $1::regclass", f"public.{table}",
        )
    }


@pytest.mark.parametrize("table", ["signals", "facts", "events"])
@pytest.mark.asyncio
async def test_the_seam_constraint_is_renamed_not_dropped(conn, table) -> None:
    """SEAMS #57 resolved, #62 opened — and the guard rail SURVIVES the
    handover under a name that says which seam it now belongs to.

    0209 named it ``readers_not_swept``; the readers are swept, but nothing
    can WRITE a history row to these three tables yet, so the CHECK is still
    the thing standing between the tree and a premature backfill. Dropping it
    on the strength of the reader sweep alone would have removed a guard that
    was doing a second job.
    """
    names = await _constraint_names(conn, table)
    assert f"{table}_origin_class_readers_not_swept" not in names
    assert f"{table}_origin_class_history_writer_not_built" in names
    assert f"{table}_origin_class_vocab" in names


@pytest.mark.parametrize(
    "table,columns,values",
    [
        ("signals",
         "(id, source_id, schema_uri, payload, origin_class)",
         "($1, 'x', 'iglu:legba/signal/jsonschema/3-0-0', '{}'::jsonb, "
         "'archive')"),
        ("facts",
         "(id, subject, predicate, value, origin_class)",
         "($1, 't0220_hist', 'y', 'z', 'archive')"),
    ],
)
@pytest.mark.asyncio
async def test_a_history_row_still_fails_naming_the_write_seam(
    conn, table, columns, values,
) -> None:
    from asyncpg.exceptions import CheckViolationError

    with pytest.raises(CheckViolationError) as exc:
        await conn.execute(
            f"INSERT INTO {table} {columns} VALUES {values}", uuid4()
        )
    assert f"{table}_origin_class_history_writer_not_built" in str(exc.value)


@pytest.mark.asyncio
async def test_the_facts_open_triple_index_is_why_the_fact_guard_stays(
    conn,
) -> None:
    """The unsolved shape SEAMS #62 owns, asserted rather than asserted-about.

    ``idx_facts_temporal_triple_open`` keys the OPEN set on
    (subject, predicate, value, valid_from) with NO origin_class leg, so an
    archived 2016 fact and a live 2026 fact asserting the same triple would
    COLLIDE at the index rather than coexist. That is a write-path design
    question, and it is the concrete reason the facts guard is still armed.
    """
    definition = await conn.fetchval(
        "SELECT pg_get_indexdef(indexrelid) FROM pg_index "
        " WHERE indexrelid = 'public.idx_facts_temporal_triple_open'::regclass"
    )
    assert definition is not None
    assert "origin_class" not in definition
    assert "superseded_by IS NULL" in definition


# ---------------------------------------------------------------------------
# Idempotency of the migration itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_apply_is_a_noop(conn) -> None:
    async with conn.transaction():
        await conn.execute(_migration_sql())
    for table in ("signals", "facts", "events"):
        names = await _constraint_names(conn, table)
        assert f"{table}_origin_class_history_writer_not_built" in names
        # And exactly ONE of it — the rename branch must not leave a second.
        assert sum(
            1 for n in names
            if n.endswith("_origin_class_history_writer_not_built")
        ) == 1, table
    assert await conn.fetchval(
        "SELECT to_regclass('public.observations_2016')"
    ) is not None
