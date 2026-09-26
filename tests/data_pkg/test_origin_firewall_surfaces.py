# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 7g-1 — the firewall on the "now" surfaces, against real SQL.

``tests/data_pkg/test_origin_gate_inventory.py`` proves the eight fenced
surfaces RENDER the origin-class leg. This file proves the leg does its job,
by planting the failure it exists to prevent and measuring that it does not
happen: **A SYNTHETIC BURST OF HISTORY.**

Ten years of holdings landing in a desk's window would be, in raw volume, the
largest arrival event in the platform's life. Every one of these surfaces
reads volume in a window and compares it to a trailing baseline, so without
the leg the act of LOADING history would read as history happening:

* the desk baseline's 24h bucket vector — the sigma the edge detector
  measures every later reading against;
* the alert trigger scan's current window and its trailing buckets — the
  edge detector itself;
* the geo convergence scan — a place suddenly converging;
* the salience batch — unscored rows queued for a model that would rank
  imported history into every window downstream.

Each test seeds a SMALL live population and a LARGE history population in the
same geo and window, then runs the module's OWN query string and asserts the
number it gets back is the live number. The history rows are written with the
``*_origin_class_history_writer_not_built`` CHECK dropped inside a
transaction that is always rolled back — the only way to get a history signal
into a table that (correctly) refuses one, and it leaves nothing behind.
"""

from __future__ import annotations

from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers.alert_trigger_scan import (
    _SIGNAL_BUCKETS_SQL,
    _SIGNAL_CURRENT_SQL,
)
from legba.data.analysts.deterministic_handlers.desk_baseline import (
    _SIGNAL_BUCKET_VECTOR_SQL,
)
from legba.data.analysts.deterministic_handlers.geo_convergence_scan import (
    _COUNTRY_SIGNALS_SQL,
)
from legba.data.analysts.signal_salience import _SELECT_BATCH_SQL
from legba.data.config import PostgresConfig

#: A geo code no other test uses, so the counts below are exact rather than
#: "at least" — a firewall proof that could only say "not more than before"
#: would not notice a leak smaller than the ambient noise.
_GEO = "ZX"

_LIVE_ROWS = 3
_HISTORY_ROWS = 40          # the burst: >10x the live population


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    yield c
    await c.close()


_INSERT_SIGNAL = """
    INSERT INTO signals (id, source_id, schema_uri, payload, geo,
                         fetched_at, created_at, origin_class)
    VALUES ($1, $2, 'iglu:legba/signal/jsonschema/3-0-0',
            $3::jsonb, ARRAY[$4], now(), now(), $5)
"""


async def _seed(conn, *, origin_class: str, count: int, source_id: str) -> None:
    for i in range(count):
        await conn.execute(
            _INSERT_SIGNAL,
            uuid4(),
            source_id,
            f'{{"title": "{origin_class} row {i}"}}',
            _GEO,
            origin_class,
        )


async def _drop_history_guard(conn) -> None:
    await conn.execute(
        "ALTER TABLE signals "
        "DROP CONSTRAINT signals_origin_class_history_writer_not_built"
    )


@pytest_asyncio.fixture
async def burst(conn):
    """A small live population and a 40-row history burst, in one geo.

    Everything happens inside a transaction that is ALWAYS rolled back: the
    history rows are only insertable with the guard dropped, and neither the
    rows nor the DDL may survive the test.
    """
    tx = conn.transaction()
    await tx.start()
    try:
        await _seed(conn, origin_class="live", count=_LIVE_ROWS,
                    source_id="source.firewall.live")
        await _drop_history_guard(conn)
        await _seed(conn, origin_class="archive", count=_HISTORY_ROWS,
                    source_id="source.firewall.holding")
        yield conn
    finally:
        await tx.rollback()


@pytest.mark.asyncio
async def test_the_burst_is_really_there(burst) -> None:
    """The proof's own control.

    Every assertion below is "the surface saw 3, not 43". That means nothing
    unless 43 rows are actually in the table, so this test is what stops the
    others from passing on an empty fixture.
    """
    total = await burst.fetchval(
        "SELECT count(*) FROM signals WHERE geo && ARRAY[$1]", _GEO
    )
    assert total == _LIVE_ROWS + _HISTORY_ROWS
    history = await burst.fetchval(
        "SELECT count(*) FROM signals "
        " WHERE geo && ARRAY[$1] AND origin_class = 'archive'", _GEO
    )
    assert history == _HISTORY_ROWS


@pytest.mark.asyncio
async def test_the_desk_baseline_sigma_does_not_move(burst) -> None:
    """The 24h bucket vector — the baseline every later reading is measured
    against. A single backfilled bucket moves both the mean and the sigma."""
    buckets = await burst.fetchval(_SIGNAL_BUCKET_VECTOR_SQL, [_GEO], 7)
    assert buckets[0] == _LIVE_ROWS
    assert sum(buckets) == _LIVE_ROWS


@pytest.mark.asyncio
async def test_the_alert_edge_detector_does_not_fire_on_a_load(burst) -> None:
    """The current window and the trailing buckets — the edge detector."""
    current = await burst.fetchval(_SIGNAL_CURRENT_SQL, [_GEO])
    assert current == float(_LIVE_ROWS)
    # (current, mean, sigma) over the 24h buckets — the edge detector's whole
    # input. The trailing buckets hold nothing, so the sigma is 0 and a
    # leaked burst would make `current` an unbounded step above the mean.
    row = await burst.fetchrow(_SIGNAL_BUCKETS_SQL, [_GEO], 7)
    assert row["current"] == float(_LIVE_ROWS)
    assert row["mean"] == 0.0
    assert row["sigma"] == 0.0


@pytest.mark.asyncio
async def test_geo_convergence_does_not_see_a_place_converging(burst) -> None:
    rows = await burst.fetch(_COUNTRY_SIGNALS_SQL, 24, 500)
    mine = [r for r in rows if r["country"] == _GEO]
    assert len(mine) == _LIVE_ROWS
    assert all(r["source_id"] == "source.firewall.live" for r in mine)


@pytest.mark.asyncio
async def test_the_salience_batch_never_queues_a_holding(burst) -> None:
    """Salience is scored by a MODEL. A history row reaching this batch would
    cost tokens AND put imported history into every ranked window downstream.
    """
    rows = await burst.fetch(_SELECT_BATCH_SQL, 24, 500)
    ids = {r["id"] for r in rows}
    history_ids = {
        r["id"]
        for r in await burst.fetch(
            "SELECT id FROM signals "
            " WHERE geo && ARRAY[$1] AND origin_class = 'archive'", _GEO
        )
    }
    assert history_ids, "the fixture planted no history rows"
    assert not (ids & history_ids)


@pytest.mark.asyncio
async def test_without_the_leg_the_burst_WOULD_have_read_as_a_surge(
    burst,
) -> None:
    """The counterfactual, measured rather than asserted.

    A firewall test that only ever reports "the number did not move" cannot
    distinguish a working gate from a query that reads nothing at all. This
    runs the SAME window with the origin-class leg stripped out and shows the
    number the fenced surfaces would have seen: 43 against a live population
    of 3 — a 14x step that is exactly what an edge detector fires on.
    """
    ungated = _SIGNAL_CURRENT_SQL.replace(
        "AND origin_class IN ('live','web_retrieval','seed')\n", ""
    )
    assert ungated != _SIGNAL_CURRENT_SQL, "the leg was not found to strip"
    without_the_leg = await burst.fetchval(ungated, [_GEO])
    with_the_leg = await burst.fetchval(_SIGNAL_CURRENT_SQL, [_GEO])
    assert without_the_leg == float(_LIVE_ROWS + _HISTORY_ROWS)
    assert with_the_leg == float(_LIVE_ROWS)
    assert without_the_leg > 10 * with_the_leg
