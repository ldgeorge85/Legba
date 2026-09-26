# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""T2.3 — the journal's salience-stratified SECOND FETCH LEG.

Three things are pinned here and they are not the same thing:

  * **The gates.** ``LEGBA_JOURNAL_SLICE_V2`` (default OFF) AND
    ``identity.kind == 'journal_assessor'``. Either one missing and the reader
    takes the recency leg.
  * **Flag-off byte identity of the SQL.** The statement ``_read_substrate_slice``
    hands Postgres at flag-off — and the statement it hands Postgres for a
    NON-journal analyst at flag-ON — is the pre-change literal, character for
    character. Pinned against a verbatim copy of that literal (``_LEG1_SQL``
    below), not against a re-derivation, so a whitespace drift in the reader
    turns this red.
  * **The selection itself**, over a seeded pool in the test Postgres: each
    stratum admits what it claims to, the caps bind, a row belongs to exactly
    one stratum, and the same pool always yields the same list in the same
    order.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.runtime.actor_substrate_slice import _SLICE_COLUMNS, _read_substrate_slice
from legba.runtime.journal_slice_v2 import (
    HIGH_BAR,
    JOURNAL_KIND,
    JOURNAL_SLICE_V2_ENV,
    SOURCE_TOP_K,
    journal_slice_v2_applies,
    journal_slice_v2_enabled,
    journal_slice_v2_sql,
    journal_v2_sql_for,
)

_SRC = "source.t23.leg2_fixture"


# ---------------------------------------------------------------------------
# descriptors + a conn that records the statement instead of running it
# ---------------------------------------------------------------------------


def _descriptor(kind: str, *, window_hours: int | None = None) -> SimpleNamespace:
    sub = SimpleNamespace(
        substrate={},
        targets=SimpleNamespace(time_window=f"{window_hours}h" if window_hours else None),
        time_window=None,
        time_window_hours=None,
    )
    return SimpleNamespace(
        identity=SimpleNamespace(id="fixture", kind=kind), subscription=sub,
    )


class _RecordingConn:
    """Captures the statement text; returns no rows so the reader short-circuits."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def fetch(self, sql, *args):
        self.statements.append(sql)
        return []

    async def fetchrow(self, sql, *args):
        return None


#: The recency leg's statement, copied VERBATIM out of
#: ``actor_substrate_slice._read_substrate_slice`` — including the leading
#: newline, the 8-space body indentation and the trailing 8-space line the
#: triple-quoted f-string produces. Do not "tidy" this; its exact bytes ARE the
#: assertion.
_LEG1_SQL = (
    "\n"
    f"        SELECT {_SLICE_COLUMNS}\n"
    "        FROM signals\n"
    "        {where}\n"
    "        ORDER BY fetched_at DESC\n"
    "        LIMIT {fetch_limit}\n"
    "        "
)


def _expected_leg1(where: str, fetch_limit: int) -> str:
    return _LEG1_SQL.replace("{where}", where).replace(
        "{fetch_limit}", str(fetch_limit)
    )


def _where_of(statement: str) -> str:
    """The one-line WHERE the reader assembled, without its indentation."""
    for line in statement.splitlines():
        if line.strip().startswith("WHERE "):
            return line.strip()
    raise AssertionError(f"no WHERE line in:\n{statement}")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(JOURNAL_SLICE_V2_ENV, raising=False)
    # The graph-structure leg issues a second statement that has nothing to do
    # with this one; switch it off so the recorded list is the signal read alone.
    monkeypatch.setenv("LEGBA_SLICE_GRAPH_STRUCTURE_CAP", "0")


# ---------------------------------------------------------------------------
# the gates
# ---------------------------------------------------------------------------


def test_flag_defaults_off():
    assert journal_slice_v2_enabled() is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", "  1  "])
def test_flag_truthy_forms(monkeypatch, raw):
    monkeypatch.setenv(JOURNAL_SLICE_V2_ENV, raw)
    assert journal_slice_v2_enabled() is True


@pytest.mark.parametrize("raw", ["", "0", "false", "FALSE", "no", "off"])
def test_flag_falsy_forms(monkeypatch, raw):
    monkeypatch.setenv(JOURNAL_SLICE_V2_ENV, raw)
    assert journal_slice_v2_enabled() is False


def test_applies_needs_both_gates(monkeypatch):
    journal = _descriptor(JOURNAL_KIND)
    desk = _descriptor("inline_target")
    assert journal_slice_v2_applies(journal) is False       # flag off
    monkeypatch.setenv(JOURNAL_SLICE_V2_ENV, "1")
    assert journal_slice_v2_applies(journal) is True
    assert journal_slice_v2_applies(desk) is False          # wrong kind
    assert journal_slice_v2_applies(SimpleNamespace()) is False
    assert journal_slice_v2_applies(None) is False


def test_sql_for_returns_none_unless_both_gates(monkeypatch):
    kw = dict(columns=_SLICE_COLUMNS, where="WHERE TRUE", total=360,
              row_cap=120, per_source_cap=15)
    assert journal_v2_sql_for(_descriptor(JOURNAL_KIND), **kw) is None
    monkeypatch.setenv(JOURNAL_SLICE_V2_ENV, "1")
    assert journal_v2_sql_for(_descriptor("deterministic"), **kw) is None
    sql = journal_v2_sql_for(_descriptor(JOURNAL_KIND), **kw)
    assert isinstance(sql, str) and "WITH pool AS" in sql


def test_sql_carries_callers_where_and_columns_verbatim():
    where = "WHERE fetched_at > NOW() - INTERVAL '24 hours' AND source_id = ANY($1)"
    sql = journal_slice_v2_sql(columns=_SLICE_COLUMNS, where=where, total=360,
                               row_cap=120, per_source_cap=15)
    assert where in sql
    assert _SLICE_COLUMNS in sql
    # The leg introduces no parameters of its own — the caller's ``*params`` are
    # the only ones, so it can ride the caller's existing ``conn.fetch`` call.
    assert sql.count("$") == where.count("$")
    for literal in (str(HIGH_BAR), "LIMIT 360", "_v2_slot <= 120",
                    f"_v2_rn_src <= {SOURCE_TOP_K}"):
        assert literal in sql, literal


# ---------------------------------------------------------------------------
# flag-off (and non-journal) byte identity of the executed statement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [JOURNAL_KIND, "inline_target", "deterministic"])
async def test_flag_off_statement_is_the_pre_change_literal(kind):
    conn = _RecordingConn()
    await _read_substrate_slice(
        conn, descriptor=_descriptor(kind), target_filter=None,
    )
    where = (
        "WHERE fetched_at > NOW() - INTERVAL '24 hours' "
        "AND (payload->>'event_class') IS DISTINCT FROM 'backfill' "
        "AND (canonical_signal_id IS NULL OR canonical_signal_id = id)"
    )
    assert len(conn.statements) == 1
    got = conn.statements[0]
    # The clause list depends on LEGBA_RESEARCH_EVIDENCE, which this test does
    # not pin — compare the SHAPE around whatever WHERE the reader built.
    assert got == _expected_leg1(_where_of(got), 360)
    assert "WITH pool AS" not in got
    assert got.rstrip().endswith("LIMIT 360")
    assert where.split(" AND ")[0] in got


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["inline_target", "deterministic", "critic",
                                  "meta_findings_synthesizer"])
async def test_other_analysts_untouched_at_flag_on(monkeypatch, kind):
    """The DESK leg's SQL text with the flag ON is the recency literal."""
    monkeypatch.setenv(JOURNAL_SLICE_V2_ENV, "1")
    conn = _RecordingConn()
    await _read_substrate_slice(
        conn, descriptor=_descriptor(kind, window_hours=72), target_filter=None,
    )
    got = conn.statements[0]
    assert got == _expected_leg1(_where_of(got), 360)
    assert "72 hours" in got
    assert "_v2_" not in got


@pytest.mark.asyncio
async def test_journal_statement_changes_only_at_flag_on(monkeypatch):
    monkeypatch.setenv(JOURNAL_SLICE_V2_ENV, "1")
    conn = _RecordingConn()
    await _read_substrate_slice(
        conn, descriptor=_descriptor(JOURNAL_KIND), target_filter=None,
    )
    got = conn.statements[0]
    assert "WITH pool AS" in got and "_v2_stratum" in got
    # same window clause, same budget — only the SELECTION differs
    assert "INTERVAL '24 hours'" in got
    assert "LIMIT 360" in got


# ---------------------------------------------------------------------------
# the selection, over a seeded pool
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
    )
    # The production pool registers this codec unconditionally
    # (``PostgresStore._init_connection``), so a bare connection here would hand
    # the reader ``salience`` as a str and the test would exercise a path the
    # actor never takes. Register it, so this traverses the real binding.
    await PostgresStore._init_connection(c)
    try:
        await c.execute("DELETE FROM signals WHERE source_id LIKE $1", _SRC + "%")
        yield c
    finally:
        await c.execute("DELETE FROM signals WHERE source_id LIKE $1", _SRC + "%")
        await c.close()


async def _seed(conn, rows):
    """``rows`` = (source_suffix, minutes_ago, magnitude|None, authority)."""
    now = datetime.now(timezone.utc)
    out, batch = [], []
    for suffix, minutes, mag, auth in rows:
        rid = uuid4()
        sal = (
            None if mag is None
            else json.dumps({"magnitude": mag, "authority": auth})
        )
        batch.append((
            rid, f"{_SRC}.{suffix}", now - timedelta(minutes=minutes),
            json.dumps({"title": f"{suffix}-{minutes}-{mag}"}), sal,
        ))
        out.append((str(rid), suffix, minutes, mag))
    await conn.executemany(
        "INSERT INTO signals (id, source_id, source_version, fetched_at, "
        "payload, owner_tenant, modality, schema_uri, salience) "
        "VALUES ($1, $2, 'v1', $3, $4, 'default', 'text', "
        "'legba/signal/1.0.0', $5::jsonb)",
        batch,
    )
    return out


_WHERE = f"WHERE source_id LIKE '{_SRC}%'"


async def _leg2(conn, *, total=12, row_cap=4, per_source_cap=2, fresh_cap=3):
    sql = journal_slice_v2_sql(
        columns=_SLICE_COLUMNS, where=_WHERE, total=total, row_cap=row_cap,
        per_source_cap=per_source_cap, fresh_cap=fresh_cap,
    )
    return [dict(r) for r in await conn.fetch(sql)]


@pytest.mark.asyncio
async def test_stratum_a_takes_the_high_bar_first(conn):
    """A loud row 20 h old beats a quiet row a minute old — the whole point."""
    await _seed(conn, [
        ("loud", 1200, 0.95, "official"),
        ("loud", 1201, 0.90, "reporting"),
        ("quiet", 1, 0.10, "reporting"),
        ("quiet", 2, 0.10, "reporting"),
    ])
    rows = await _leg2(conn, total=2, row_cap=2, per_source_cap=2, fresh_cap=1)
    mags = [r["salience"]["magnitude"] for r in rows]
    assert max(mags) == 0.95
    assert 0.95 in mags


@pytest.mark.asyncio
async def test_stratum_a_honours_the_per_source_cap(conn):
    """Six loud rows from ONE source, cap 2 — the firehose gets 2 slots in A."""
    await _seed(conn, [("fire", 100 + i, 0.9, "reporting") for i in range(6)]
                + [("other", 500, 0.85, "reporting")])
    rows = await _leg2(conn, total=3, row_cap=3, per_source_cap=2, fresh_cap=1)
    by_src: dict[str, int] = {}
    for r in rows:
        by_src[r["source_id"]] = by_src.get(r["source_id"], 0) + 1
    assert by_src[f"{_SRC}.other"] == 1
    # A is capped at 2/source; the other slot cannot be a third 'fire' row
    # admitted BY STRATUM A. (D carries the same guard, so 2 is the ceiling.)
    assert by_src[f"{_SRC}.fire"] <= 2


@pytest.mark.asyncio
async def test_stratum_b_gives_every_source_a_slot(conn):
    """One loud source and four silent ones: each still lands its top row."""
    await _seed(conn,
                [("loud", 100 + i, 0.9, "reporting") for i in range(10)]
                + [(f"s{i}", 200 + i, 0.2, "reporting") for i in range(4)])
    rows = await _leg2(conn, total=8, row_cap=6, per_source_cap=2, fresh_cap=1)
    srcs = {r["source_id"] for r in rows}
    for i in range(4):
        assert f"{_SRC}.s{i}" in srcs, f"source s{i} missing from the leg"


@pytest.mark.asyncio
async def test_stratum_c_reserves_the_newest_rows(conn):
    """A fresh UNSCORED row survives a pool of loud old ones."""
    await _seed(conn, [("old", 600 + i, 0.9, "official") for i in range(20)])
    fresh = await _seed(conn, [("new", 1, None, None), ("new", 2, None, None)])
    rows = await _leg2(conn, total=6, row_cap=3, per_source_cap=3, fresh_cap=2)
    ids = {str(r["id"]) for r in rows}
    assert {fresh[0][0], fresh[1][0]} <= ids


@pytest.mark.asyncio
async def test_no_row_is_delivered_twice(conn):
    """Dedup across strata: a row qualifying for A, B, C and D appears once."""
    await _seed(conn, [("solo", 1, 0.99, "official")]
                + [("bulk", 100 + i, 0.5, "reporting") for i in range(20)])
    rows = await _leg2(conn, total=12, row_cap=6, per_source_cap=6, fresh_cap=4)
    ids = [str(r["id"]) for r in rows]
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
async def test_total_never_exceeds_the_budget(conn):
    await _seed(conn, [(f"s{i % 5}", i, 0.1 + (i % 9) / 10, "reporting")
                       for i in range(80)])
    for total in (5, 12, 40):
        rows = await _leg2(conn, total=total, row_cap=4, per_source_cap=3,
                           fresh_cap=3)
        assert len(rows) <= total
    # and it FILLS the budget when the pool is big enough
    assert len(await _leg2(conn, total=40, row_cap=4, per_source_cap=3,
                           fresh_cap=3)) == 40


@pytest.mark.asyncio
async def test_order_is_deterministic(conn):
    """Same pool, same list, same order — three runs. Ties break on id, so a
    plan change inside Postgres cannot reshuffle the narrator's window."""
    await _seed(conn, [(f"s{i % 4}", i, round(0.5, 2), "reporting")
                       for i in range(30)])
    runs = [[str(r["id"]) for r in await _leg2(conn, total=15, row_cap=6,
                                               per_source_cap=4, fresh_cap=4)]
            for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


@pytest.mark.asyncio
async def test_interleave_spreads_the_strata_across_the_prefix(conn):
    """The caller cuts the leg's output to ``row_cap`` — so the freshness
    reserve has to be present in the PREFIX, not parked at the tail."""
    await _seed(conn, [("loud", 500 + i, 0.9, "official") for i in range(40)])
    fresh = await _seed(conn, [("new", i, None, None) for i in range(1, 7)])
    rows = await _leg2(conn, total=24, row_cap=12, per_source_cap=12,
                       fresh_cap=6)
    prefix = {str(r["id"]) for r in rows[:12]}
    assert len(prefix & {f[0] for f in fresh}) >= 2


@pytest.mark.asyncio
async def test_reader_uses_the_leg_end_to_end(conn, monkeypatch):
    """Through ``_read_substrate_slice`` itself, and this is THE defect in
    miniature: one loud row 20 h into a 48 h window, behind 400 fresher routine
    rows. The recency fetch stops at 200 (the reader's ``max(200, cap*3)``
    floor) and never sees it; the stratified leg leads with it."""
    loud = (await _seed(conn, [("loud", 1200, 0.95, "official")]))[0][0]
    await _seed(conn, [("noise", 1 + i, 0.05, "reporting") for i in range(400)])
    monkeypatch.setenv("LEGBA_SLICE_ROW_CAP", "20")
    monkeypatch.setenv("LEGBA_GLOBAL_SLICE_PER_SOURCE_CAP", "20")
    desc = _descriptor(JOURNAL_KIND, window_hours=48)

    off = await _read_substrate_slice(conn, descriptor=desc, target_filter=None)
    assert loud not in {str(r["id"]) for r in off}
    assert all((r.get("salience") or {}).get("magnitude", 0) < 0.5 for r in off)

    monkeypatch.setenv(JOURNAL_SLICE_V2_ENV, "1")
    on = await _read_substrate_slice(conn, descriptor=desc, target_filter=None)
    assert loud in {str(r["id"]) for r in on}
    assert len(on) == len(off)

    # ... and a DESK on the same pool is unmoved by the flag.
    desk = _descriptor("inline_target", window_hours=48)
    monkeypatch.delenv(JOURNAL_SLICE_V2_ENV)
    desk_off = await _read_substrate_slice(conn, descriptor=desk, target_filter=None)
    monkeypatch.setenv(JOURNAL_SLICE_V2_ENV, "1")
    desk_on = await _read_substrate_slice(conn, descriptor=desk, target_filter=None)
    assert [r["id"] for r in desk_off] == [r["id"] for r in desk_on]


@pytest.mark.asyncio
async def test_flag_off_rows_are_identical_on_a_seeded_pool(conn, monkeypatch):
    """Flag-off identity, row for row and order for order, with the flag ON for
    a non-journal descriptor in between — the reader must not carry state."""
    await _seed(conn, [(f"s{i % 6}", i, (i % 10) / 10, "reporting")
                       for i in range(60)])
    monkeypatch.setenv("LEGBA_SLICE_ROW_CAP", "10")
    desc = _descriptor(JOURNAL_KIND, window_hours=48)
    first = await _read_substrate_slice(conn, descriptor=desc, target_filter=None)
    monkeypatch.setenv(JOURNAL_SLICE_V2_ENV, "1")
    await _read_substrate_slice(
        conn, descriptor=_descriptor("deterministic", window_hours=48),
        target_filter=None,
    )
    monkeypatch.delenv(JOURNAL_SLICE_V2_ENV)
    second = await _read_substrate_slice(conn, descriptor=desc, target_filter=None)
    assert [r["id"] for r in first] == [r["id"] for r in second]
    assert [r["fetched_at"] for r in first] == [r["fetched_at"] for r in second]
