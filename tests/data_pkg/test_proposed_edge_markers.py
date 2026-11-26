# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Finally refused relationship candidates keep only a marker.

The migration, the trigger that writes the marker, the producer's skip of a
marked pair, the entity-merge and cut-out readers that had to learn about the
marker table, and the one-off script that moves the older rows. The database
tests run in their own freshly migrated database (the script deletes final-state
rows wholesale, which must never reach the shared session database).
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.migrations import MIGRATIONS_DIR, migration_sort_key

REPO = Path(__file__).resolve().parents[2]
MIGRATION = MIGRATIONS_DIR / "20261125T1000_proposed_edge_markers.sql"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Pure checks
# ---------------------------------------------------------------------------


def test_the_migration_is_time_named_and_sorts_last():
    names = [p.name for p in MIGRATIONS_DIR.glob("*.sql")]
    assert MIGRATION.name in names
    migration_sort_key(MIGRATION.name)
    assert max(names, key=migration_sort_key) == MIGRATION.name


def test_the_migration_is_create_only_and_repeatable():
    body = MIGRATION.read_text()
    code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("--"))
    low = code.lower()
    assert "create table if not exists public.proposed_edge_markers" in low
    assert "create index if not exists" in low
    assert "create or replace function" in low
    assert low.count("drop trigger if exists") == 2
    for verb in ("delete from public.proposed_edges", "truncate", "drop table", "begin;", "commit;"):
        assert verb not in low
    # it never backfills: no insert from the candidates table
    assert "from public.proposed_edges" not in low


def test_the_script_is_dry_by_default_and_checks_its_arguments(monkeypatch, capsys):
    s = _load_script("marker_final_rejections")
    args = s.build_parser().parse_args([])
    assert args.apply is False and args.max_rows is None
    assert list(args.statuses) == ["rejected", "retired"]
    monkeypatch.setattr(sys, "argv", ["x", "--batch", "0"])
    assert s.main() == 2


def test_the_delete_cannot_act_without_a_marker():
    s = _load_script("marker_final_rejections")
    sql = s.DELETE_MARKED_SQL
    assert "EXISTS" in sql and "proposed_edge_markers" in sql
    assert "pe.id <> ALL" in sql and "pe.status = ANY" in sql
    for sql in (s.INSERT_MARKERS_SQL, s.NEXT_BATCH_SQL):
        assert "DELETE" not in sql.upper()


def test_the_producer_skips_a_marked_pair_in_its_sql():
    from legba.data.analysts.deterministic_handlers.entity_resolution import CO_OCCURS_UPSERT_SQL as sql
    assert "NOT EXISTS" in sql and "proposed_edge_markers" in sql
    assert "ON CONFLICT (lower(source_entity), lower(target_entity), relationship_type)" in sql


# ---------------------------------------------------------------------------
# A private, migrated database
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="module")
async def scratch_cfg():
    from legba.data.config import PostgresConfig
    from legba.data.migrate import apply_primary_migrations
    from tests._pg_test_server_guard import assert_test_server

    await assert_test_server()
    admin_dsn = "postgresql://legba:legba@127.0.0.1:5432/postgres"
    name = f"legba_marker_{uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    cfg = PostgresConfig(host="127.0.0.1", port=5432, user="legba",
                         password="legba", database=name)
    try:
        await apply_primary_migrations(cfg)
        yield cfg
    finally:
        admin = await asyncpg.connect(admin_dsn)
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{name}' AND pid <> pg_backend_pid()")
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await admin.close()


@pytest_asyncio.fixture
async def conn(scratch_cfg):
    c = await asyncpg.connect(scratch_cfg.dsn)
    for t in ("output_consumption",):
        await c.execute(f"DELETE FROM {t}")
    await c.execute("DELETE FROM proposed_edges")
    await c.execute("DELETE FROM proposed_edge_markers")
    yield c
    await c.close()


def _env(monkeypatch, cfg):
    monkeypatch.setenv("LEGBA_DATA_PG_HOST", cfg.host)
    monkeypatch.setenv("LEGBA_DATA_PG_PORT", str(cfg.port))
    monkeypatch.setenv("LEGBA_DATA_PG_USER", cfg.user)
    monkeypatch.setenv("LEGBA_DATA_PG_PASSWORD", cfg.password)
    monkeypatch.setenv("LEGBA_DATA_PG_DB", cfg.database)


async def _edge(c, a, b, status="pending", rel="co_occurs", conf=0.6):
    return await c.fetchval(
        "INSERT INTO proposed_edges (source_entity, target_entity, relationship_type, "
        "confidence, evidence_text, status, reviewed_at) VALUES ($1,$2,$3,$4,'ev',$5, now()) "
        "RETURNING id", a, b, rel, conf, status)


async def _marker(c, a, b, rel="co_occurs"):
    return await c.fetchrow(
        "SELECT * FROM proposed_edge_markers WHERE source_key=lower($1) AND target_key=lower($2) "
        "AND relationship_type=$3", a, b, rel)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_creates_the_table_key_and_triggers(conn):
    cols = {r["column_name"]: r["data_type"] for r in await conn.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name='proposed_edge_markers'")}
    assert set(cols) == {"source_key", "target_key", "relationship_type",
                         "final_status", "reached_at", "confidence"}
    pk = await conn.fetchval(
        "SELECT array_agg(a.attname ORDER BY k.ord) FROM pg_index i "
        "JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, ord) ON true "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
        "WHERE i.indrelid = 'proposed_edge_markers'::regclass AND i.indisprimary")
    assert pk == ["source_key", "target_key", "relationship_type"]
    trg = {r["tgname"] for r in await conn.fetch(
        "SELECT tgname FROM pg_trigger WHERE tgrelid='proposed_edges'::regclass AND NOT tgisinternal")}
    assert {"proposed_edge_marker_ins", "proposed_edge_marker_upd"} <= trg
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute("INSERT INTO proposed_edge_markers (source_key,target_key,"
                           "relationship_type,final_status) VALUES ('a','b','co_occurs','pending')")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_applies_twice_without_change(conn):
    await _edge(conn, "TwiceA", "TwiceB", "rejected")
    before = await conn.fetchval("SELECT count(*) FROM proposed_edge_markers")
    await conn.execute(MIGRATION.read_text())
    await conn.execute(MIGRATION.read_text())
    assert await conn.fetchval("SELECT count(*) FROM proposed_edge_markers") == before == 1
    n = await conn.fetchval(
        "SELECT count(*) FROM pg_trigger WHERE tgrelid='proposed_edges'::regclass "
        "AND tgname LIKE 'proposed_edge_marker_%'")
    assert n == 2


# ---------------------------------------------------------------------------
# The trigger
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_row_that_becomes_final_gets_a_lowercase_marker(conn):
    pid = await _edge(conn, "Alpha Corp", "BETA Ltd", "pending", conf=0.7)
    assert await _marker(conn, "Alpha Corp", "BETA Ltd") is None
    await conn.execute("UPDATE proposed_edges SET status='rejected', reviewed_at=now() WHERE id=$1", pid)
    m = await _marker(conn, "Alpha Corp", "BETA Ltd")
    assert (m["source_key"], m["target_key"], m["final_status"]) == ("alpha corp", "beta ltd", "rejected")
    assert abs(m["confidence"] - 0.7) < 1e-6 and m["reached_at"] is not None
    r = await _edge(conn, "Gamma", "Delta", "retired")        # inserted final
    assert (await _marker(conn, "Gamma", "Delta"))["final_status"] == "retired"
    assert r is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_other_states_get_no_marker(conn):
    for st in ("pending", "promoted", "orphaned", "merged"):
        await _edge(conn, f"S-{st}", "Other", st)
    assert await conn.fetchval("SELECT count(*) FROM proposed_edge_markers") == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_row_that_leaves_a_final_state_loses_its_marker(conn):
    pid = await _edge(conn, "Eps", "Zeta", "rejected")
    assert await _marker(conn, "Eps", "Zeta")
    await conn.execute("UPDATE proposed_edges SET status='pending' WHERE id=$1", pid)
    assert await _marker(conn, "Eps", "Zeta") is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_final_row_moved_to_new_endpoints_marks_the_new_pair(conn):
    pid = await _edge(conn, "OldName", "Peer", "rejected")
    await conn.execute("UPDATE proposed_edges SET source_entity='NewName' WHERE id=$1", pid)
    assert await _marker(conn, "NewName", "Peer")
    assert await _marker(conn, "OldName", "Peer")      # the old pair stays refused


# ---------------------------------------------------------------------------
# No re-proposal
# ---------------------------------------------------------------------------


async def _propose(c, a, b):
    from legba.data.analysts.deterministic_handlers.entity_resolution import CO_OCCURS_UPSERT_SQL
    return await c.execute(CO_OCCURS_UPSERT_SQL, a, b, "snippet", [])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_marked_pair_is_not_proposed_again_even_with_its_row_gone(conn):
    pid = await _edge(conn, "Iran", "Israel", "rejected")
    # while the full row exists the unique key holds (status stays rejected)
    await _propose(conn, "Iran", "Israel")
    assert await conn.fetchval("SELECT status FROM proposed_edges WHERE id=$1", pid) == "rejected"
    # remove the full row: only the marker is left, and the pair still is not proposed
    await conn.execute("DELETE FROM proposed_edges WHERE id=$1", pid)
    for a, b in (("Iran", "Israel"), ("IRAN", "israel")):
        await _propose(conn, a, b)
    assert await conn.fetchval("SELECT count(*) FROM proposed_edges") == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unmarked_pairs_are_proposed_and_accrue_as_before(conn):
    await _propose(conn, "Fresh A", "Fresh B")
    await _propose(conn, "fresh a", "FRESH B")
    row = await conn.fetchrow("SELECT status, confidence FROM proposed_edges")
    assert row["status"] == "pending" and abs(row["confidence"] - 0.45) < 1e-6
    assert await conn.fetchval("SELECT count(*) FROM proposed_edges") == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_marker_for_another_relationship_type_does_not_block(conn):
    pid = await _edge(conn, "Pair X", "Pair Y", "rejected", rel="allied_with")
    await conn.execute("DELETE FROM proposed_edges WHERE id=$1", pid)
    await _propose(conn, "Pair X", "Pair Y")                  # co_occurs: a different key
    assert await conn.fetchval("SELECT count(*) FROM proposed_edges") == 1


# ---------------------------------------------------------------------------
# Readers that learned about the marker table
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cut_out_search_counts_markers_like_the_rows_they_replace(conn):
    from legba.data.analysts.relationship_reifier import _intermediary_candidates_for

    async def seed():
        # Hub is co-mentioned with both ends: one live edge each, one refused each
        await _edge(conn, "Hub", "Anchor One", "pending", conf=0.5)
        await _edge(conn, "Hub", "Anchor Two", "pending", conf=0.5)
        await _edge(conn, "Hub Alt", "Anchor One", "rejected", conf=0.5)
        await _edge(conn, "Hub Alt", "Anchor Two", "retired", conf=0.5)
        await _edge(conn, "Anchor One", "Lonely", "rejected", conf=0.5)

    await seed()
    full = await _intermediary_candidates_for(conn, source="Anchor One", target="Anchor Two", limit=5)
    # remove the finally refused full rows, as the script does
    await conn.execute("DELETE FROM proposed_edges WHERE status IN ('rejected','retired')")
    assert await conn.fetchval("SELECT count(*) FROM proposed_edge_markers") == 3
    after = await _intermediary_candidates_for(conn, source="Anchor One", target="Anchor Two", limit=5)
    assert [x.lower() for x in after] == [x.lower() for x in full] == ["hub", "hub alt"]
    assert after[0] == "Hub"                          # the cased live spelling wins


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_merge_does_not_reopen_a_refused_pair(conn):
    from legba.data.analysts.deterministic_handlers.entity_gc import _repoint_proposed_edges

    # the keeper pair was refused and its full row is gone; a live row under the
    # merged-away name would otherwise be moved onto it
    gone = await _edge(conn, "Keeper", "Other", "rejected")
    await conn.execute("DELETE FROM proposed_edges WHERE id=$1", gone)
    live = await _edge(conn, "Loser", "Other", "pending")
    # and a marker that names the loser moves to the keeper
    await _edge(conn, "Loser", "Third", "rejected")
    await conn.execute("DELETE FROM proposed_edges WHERE source_entity='Loser' AND target_entity='Third'")
    await _repoint_proposed_edges(conn, "Loser", "Keeper")
    assert await conn.fetchval("SELECT status FROM proposed_edges WHERE id=$1", live) == "merged"
    assert await _marker(conn, "Keeper", "Third")
    assert await _marker(conn, "Loser", "Third") is None
    # a refused pair that would become a self-loop is dropped, not moved
    await conn.execute("INSERT INTO proposed_edge_markers (source_key,target_key,relationship_type,"
                       "final_status) VALUES ('loser','keeper','co_occurs','rejected')")
    await _repoint_proposed_edges(conn, "Loser", "Keeper")
    assert await _marker(conn, "Loser", "Keeper") is None
    assert await _marker(conn, "Keeper", "Keeper") is None


# ---------------------------------------------------------------------------
# The one-off script
# ---------------------------------------------------------------------------


async def _old_row(c, a, b, status):
    """A final row as it was before the trigger existed: no marker."""
    pid = await _edge(c, a, b, status)
    await c.execute("DELETE FROM proposed_edge_markers WHERE source_key=lower($1) AND target_key=lower($2)", a, b)
    return pid


async def _seed(c):
    ids = {"rej": [], "ret": [], "pending": [], "held": None}
    for i in range(7):
        ids["rej"].append(await _old_row(c, f"RejA{i}", f"RejB{i}", "rejected"))
    for i in range(5):
        ids["ret"].append(await _old_row(c, f"RetA{i}", f"RetB{i}", "retired"))
    for i in range(3):
        ids["pending"].append(await _edge(c, f"PendA{i}", f"PendB{i}", "pending"))
    held = await _old_row(c, "HeldA", "HeldB", "rejected")
    await c.execute("INSERT INTO output_consumption (consumer_id, consumed_id, consumer_kind, context) "
                    "VALUES ($1,$2,'finding','cited')", uuid4(), held)
    ids["held"] = held
    return ids


async def _ids(c):
    return {r["id"] for r in await c.fetch("SELECT id FROM proposed_edges")}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_script_dry_run_writes_nothing(conn, scratch_cfg, monkeypatch, capsys):
    s = _load_script("marker_final_rejections")
    _env(monkeypatch, scratch_cfg)
    ids = await _seed(conn)
    before = await _ids(conn)
    assert await s.run(s.build_parser().parse_args(["--skip-text-scan"])) == 0
    out = capsys.readouterr().out
    assert "final-state candidates (rejected, retired): 13 rows" in out
    assert "already marked: 0; to be marked: 13" in out
    assert "HELD BACK (named by something, full row kept): 1 rows" in out
    assert "full rows that would be deleted: 12" in out and "DRY RUN" in out
    assert await _ids(conn) == before
    assert await conn.fetchval("SELECT count(*) FROM proposed_edge_markers") == 0
    assert ids["held"] in before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_script_apply_marks_then_deletes_and_is_idempotent(conn, scratch_cfg, monkeypatch, capsys):
    s = _load_script("marker_final_rejections")
    _env(monkeypatch, scratch_cfg)
    ids = await _seed(conn)
    argv = ["--apply", "--skip-text-scan", "--batch", "5", "--pause", "0"]
    assert await s.run(s.build_parser().parse_args(argv)) == 0
    out = capsys.readouterr().out
    assert "batch 3:" in out                                     # 13 rows / 5 per batch
    assert "full rows deleted 12, held back and kept 1" in out
    assert "still without a marker: 0" in out
    assert await _ids(conn) == {*ids["pending"], ids["held"]}    # live + held stay
    assert await conn.fetchval("SELECT count(*) FROM proposed_edge_markers") == 13
    assert (await _marker(conn, "RejA0", "RejB0"))["final_status"] == "rejected"
    assert (await _marker(conn, "RetA0", "RetB0"))["final_status"] == "retired"
    assert await _marker(conn, "PendA0", "PendB0") is None
    # the refused pairs cannot be proposed again
    await _propose(conn, "RejA0", "RejB0")
    assert await conn.fetchval("SELECT count(*) FROM proposed_edges WHERE source_entity='RejA0'") == 0
    # second run: nothing new
    assert await s.run(s.build_parser().parse_args(argv)) == 0
    out = capsys.readouterr().out
    assert "markers written 0, full rows deleted 0" in out
    assert await conn.fetchval("SELECT count(*) FROM proposed_edge_markers") == 13


@pytest.mark.integration
@pytest.mark.asyncio
async def test_script_resumes_after_a_partial_run(conn, scratch_cfg, monkeypatch, capsys):
    s = _load_script("marker_final_rejections")
    _env(monkeypatch, scratch_cfg)
    await _seed(conn)
    base = ["--apply", "--skip-text-scan", "--batch", "4", "--pause", "0"]
    assert await s.run(s.build_parser().parse_args(base + ["--max-rows", "6"])) == 0
    assert "looked at 6 final rows" in capsys.readouterr().out
    assert await conn.fetchval("SELECT count(*) FROM proposed_edge_markers") == 6
    assert await s.run(s.build_parser().parse_args(base)) == 0
    assert await conn.fetchval("SELECT count(*) FROM proposed_edge_markers") == 13
    assert await conn.fetchval("SELECT count(*) FROM proposed_edges WHERE status IN ('rejected','retired')") == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_script_never_deletes_a_row_without_a_marker(conn):
    s = _load_script("marker_final_rejections")
    a = await _old_row(conn, "NoMarkA", "NoMarkB", "rejected")
    b = await _edge(conn, "HasMarkA", "HasMarkB", "rejected")        # the trigger wrote its marker
    assert await s.delete_marked(conn, [a, b], [], ["rejected", "retired"]) == 1
    assert await _ids(conn) == {a}
    # a live row is never deleted, marker or not
    p = await _edge(conn, "LiveA", "LiveB", "pending")
    await conn.execute("INSERT INTO proposed_edge_markers (source_key,target_key,relationship_type,"
                       "final_status) VALUES ('livea','liveb','co_occurs','rejected')")
    assert await s.delete_marked(conn, [p], [], ["rejected", "retired"]) == 0
    # a held-back id is never deleted
    h = await _edge(conn, "HeldA2", "HeldB2", "rejected")
    assert await s.delete_marked(conn, [h], [h], ["rejected", "retired"]) == 0
