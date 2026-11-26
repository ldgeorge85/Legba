# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The three cleanup scripts that are allowed to drop data.

* ``archive_backup_tables.py`` - the nine backup tables: dry run by default, a
  drop only in the same invocation as its dump.
* ``drop_dead_relationship_candidates.py`` - final-state ``proposed_edges``
  rows: dry run, dump-verify, bounded delete, a row anything names is held back.
* ``dump_age_graph.py`` - the AGE graph dump the drop migration follows.

Nothing here touches a live database: the Postgres-backed tests build their own
rows in the isolated test database and point the scripts at it through the
``LEGBA_DATA_PG_*`` variables they read.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import importlib.util
import csv
import json
import sys
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

REPO = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# The nine backup tables
# ---------------------------------------------------------------------------


def test_backup_tables_refuse_apply_without_a_dump(capsys, monkeypatch):
    arch = _load_script("archive_backup_tables")
    monkeypatch.setattr(sys, "argv", ["archive_backup_tables.py", "--apply"])
    assert arch.main() == 2
    assert "needs --dump" in capsys.readouterr().err


def test_backup_tables_exclude_is_checked_against_the_nine():
    arch = _load_script("archive_backup_tables")
    ns = argparse.Namespace(exclude=["signal_alias_backup_n_u"])
    picked = arch._selected(ns)
    assert len(picked) == 8 and "signal_alias_backup_n_u" not in picked
    with pytest.raises(SystemExit):
        arch._selected(argparse.Namespace(exclude=["signals"]))


def test_backup_tables_sequence_is_dry_then_dump_then_drop(monkeypatch):
    """No --dump: only the dry run. --dump alone: dump, never apply. --dump
    --apply: dump FIRST, then apply, with the manifest of THAT dump."""
    arch = _load_script("archive_backup_tables")
    calls: list[str] = []
    manifest = {"tables": {}}

    async def dry(args):
        calls.append("dry")
        return 0

    async def dump(args):
        calls.append("dump")
        return Path("d"), manifest, True

    async def drop(out_dir, m):
        calls.append("apply")
        assert m is manifest
        return 0

    monkeypatch.setattr(arch, "dry_run", dry)
    monkeypatch.setattr(arch, "dump_and_verify", dump)
    monkeypatch.setattr(arch, "apply", drop)

    asyncio.run(arch.run(argparse.Namespace(dump=None, apply=False)))
    assert calls == ["dry"]
    calls.clear()
    asyncio.run(arch.run(argparse.Namespace(dump="d", apply=False)))
    assert calls == ["dump"]
    calls.clear()
    asyncio.run(arch.run(argparse.Namespace(dump="d", apply=True)))
    assert calls == ["dump", "apply"]


def test_backup_tables_a_failed_dump_drops_nothing(monkeypatch):
    arch = _load_script("archive_backup_tables")
    manifest = {"tables": {"t": {"present": True, "verified": False}}}

    async def fake_live(conn, tables=None):
        return {"t": {"present": True, "rows": 1, "md5": "x", "bytes": 1}}

    class _Conn:
        async def close(self):
            pass

        def transaction(self, **kw):  # pragma: no cover - never reached
            raise AssertionError("a drop must not start")

    async def fake_connect(read_only):
        if not read_only:
            raise AssertionError("no write connection may open for an unverified dump")
        return _Conn()

    monkeypatch.setattr(arch, "live_fingerprints", fake_live)
    monkeypatch.setattr(arch, "_connect", fake_connect)
    assert asyncio.run(arch.apply(Path("d"), manifest)) == 1


# ---------------------------------------------------------------------------
# Final-state relationship candidates
# ---------------------------------------------------------------------------


def test_candidates_script_is_dry_by_default_and_refuses_a_bare_apply(capsys, monkeypatch):
    cand = _load_script("drop_dead_relationship_candidates")
    args = cand.build_parser().parse_args([])
    assert args.dump is None and args.apply is False
    assert list(args.statuses) == ["rejected", "retired"]
    monkeypatch.setattr(sys, "argv", ["x", "--apply"])
    assert cand.main() == 2
    assert "needs --dump" in capsys.readouterr().err


def test_candidates_every_reference_check_is_a_read():
    cand = _load_script("drop_dead_relationship_candidates")
    sqls = list(cand.REFERENCE_QUERIES.values()) + [cand.TEXT_SCAN_SQL]
    for sql in sqls:
        up = sql.upper()
        assert up.lstrip().startswith("SELECT")
        for w in ("DELETE", "UPDATE ", "INSERT", "DROP", "TRUNCATE"):
            assert w not in up


@pytest_asyncio.fixture(scope="module")
async def scratch_cfg():
    """A private, fully migrated database: these tests delete final-state
    rows wholesale, which must never reach the shared session database."""
    from legba.data.config import PostgresConfig
    from legba.data.migrate import apply_primary_migrations
    from tests._pg_test_server_guard import assert_test_server

    await assert_test_server()
    admin_dsn = "postgresql://legba:legba@127.0.0.1:5432/postgres"
    name = f"legba_cand_{uuid4().hex[:10]}"
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


def _env(monkeypatch, cfg):
    monkeypatch.setenv("LEGBA_DATA_PG_HOST", cfg.host)
    monkeypatch.setenv("LEGBA_DATA_PG_PORT", str(cfg.port))
    monkeypatch.setenv("LEGBA_DATA_PG_USER", cfg.user)
    monkeypatch.setenv("LEGBA_DATA_PG_PASSWORD", cfg.password)
    monkeypatch.setenv("LEGBA_DATA_PG_DB", cfg.database)


async def _seed(conn, tag: str) -> dict[str, list]:
    """Final-state and live candidates, plus candidates something names."""
    ids: dict[str, list] = {k: [] for k in
                            ("rejected", "retired", "pending", "promoted",
                             "named_by_consumption", "named_in_text",
                             "named_by_output")}
    async def add(status, key, text="seed evidence\nwith a newline, and a comma"):
        pid = uuid4()
        await conn.execute(
            "INSERT INTO proposed_edges (id, source_entity, target_entity, "
            "relationship_type, evidence_text, status) VALUES ($1,$2,$3,'co_occurs',$4,$5)",
            pid, f"{tag}-a-{pid}", f"{tag}-b-{pid}", text, status)
        ids[key].append(pid)
        return pid

    for _ in range(7):
        await add("rejected", "rejected")
    for _ in range(5):
        await add("retired", "retired")
    for _ in range(3):
        await add("pending", "pending")
    await add("promoted", "promoted")
    c = await add("rejected", "named_by_consumption")
    await conn.execute(
        "INSERT INTO output_consumption (consumer_id, consumed_id, consumer_kind, context) "
        "VALUES ($1,$2,'finding','cited')", uuid4(), c)
    t = await add("retired", "named_in_text")
    await conn.execute(
        "INSERT INTO analyst_outputs (id, kind, title, body, schema_uri) "
        "VALUES (gen_random_uuid(),'finding','t',$1,'iglu:x/y/jsonschema/1-0-0')",
        f"a sentence that names {t} in passing")
    o = await add("rejected", "named_by_output")
    await conn.execute(
        "INSERT INTO analyst_outputs (id, kind, title, body, schema_uri, derived_from) "
        "VALUES (gen_random_uuid(),'finding','t','b','iglu:x/y/jsonschema/1-0-0',$1)",
        [o])
    return ids


async def _cleanup(conn, tag: str):
    await conn.execute("DELETE FROM proposed_edges WHERE source_entity LIKE $1", f"{tag}-%")
    await conn.execute("DELETE FROM output_consumption WHERE consumer_kind='finding' AND context='cited'")
    await conn.execute("DELETE FROM analyst_outputs WHERE title='t' AND schema_uri='iglu:x/y/jsonschema/1-0-0'")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_candidates_plan_dump_and_bounded_delete(scratch_cfg, monkeypatch, tmp_path, capsys):
    cand = _load_script("drop_dead_relationship_candidates")
    _env(monkeypatch, scratch_cfg)
    tag = f"dmg{uuid4().hex[:8]}"
    conn = await asyncpg.connect(scratch_cfg.dsn)
    try:
        # Isolate from any final-state rows other tests left behind.
        await conn.execute("DELETE FROM proposed_edges WHERE status IN ('rejected','retired','orphaned')")
        ids = await _seed(conn, tag)
        held_expect = {ids["named_by_consumption"][0], ids["named_in_text"][0],
                       ids["named_by_output"][0]}
        safe_expect = {*ids["rejected"], *ids["retired"]}

        # ---- dry run: counts, names the holds, writes nothing
        args = cand.build_parser().parse_args([])
        assert await cand.run(args) == 0
        out = capsys.readouterr().out
        assert f"SAFE to delete: {len(safe_expect)} rows" in out
        assert "HELD BACK (named by something): 3 rows" in out
        assert "output_consumption_consumed: 1" in out
        assert "analyst_outputs_derived_from: 1" in out
        assert "analyst_outputs_text_names_id: 1" in out
        assert await conn.fetchval("SELECT count(*) FROM proposed_edges WHERE source_entity LIKE $1", f"{tag}-%") == 19
        assert not list(tmp_path.iterdir())

        # ---- dump only: the file holds exactly the safe rows, nothing deleted
        args = cand.build_parser().parse_args(["--dump", str(tmp_path)])
        assert await cand.run(args) == 0
        dumped = set()
        with gzip.open(tmp_path / cand.DUMP_NAME, "rt", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                dumped.add(row["id"])
                assert row["status"] in ("rejected", "retired")
        assert dumped == {str(i) for i in safe_expect}
        assert await conn.fetchval("SELECT count(*) FROM proposed_edges WHERE source_entity LIKE $1", f"{tag}-%") == 19

        # ---- apply, bounded batches: only the dumped rows go; held and live stay
        args = cand.build_parser().parse_args(
            ["--dump", str(tmp_path), "--apply", "--batch", "5", "--pause", "0"])
        assert await cand.run(args) == 0
        left = {str(r["id"]) for r in await conn.fetch(
            "SELECT id FROM proposed_edges WHERE source_entity LIKE $1", f"{tag}-%")}
        expect_left = {str(i) for i in
                       held_expect | set(ids["pending"]) | set(ids["promoted"])}
        assert left == expect_left
        assert "batch 3:" in capsys.readouterr().out   # 12 rows / 5 per batch
    finally:
        await _cleanup(conn, tag)
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_candidates_a_dump_that_does_not_match_deletes_nothing(scratch_cfg, monkeypatch, tmp_path):
    """If the dump cannot be verified the run stops before any delete."""
    cand = _load_script("drop_dead_relationship_candidates")
    _env(monkeypatch, scratch_cfg)
    tag = f"dmg{uuid4().hex[:8]}"
    conn = await asyncpg.connect(scratch_cfg.dsn)
    try:
        await conn.execute("DELETE FROM proposed_edges WHERE status IN ('rejected','retired','orphaned')")
        ids = await _seed(conn, tag)

        real = cand.dump_safe

        async def lying_dump(conn_, p, held, out_dir, min_age):
            p = dict(p, safe_md5="0" * 32)
            return await real(conn_, p, held, out_dir, min_age)

        monkeypatch.setattr(cand, "dump_safe", lying_dump)
        args = cand.build_parser().parse_args(["--dump", str(tmp_path), "--apply"])
        with pytest.raises(SystemExit, match="dump verification FAILED"):
            await cand.run(args)
        assert await conn.fetchval(
            "SELECT count(*) FROM proposed_edges WHERE source_entity LIKE $1", f"{tag}-%") == 19
        assert ids["rejected"]
    finally:
        await _cleanup(conn, tag)
        await conn.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_candidates_min_age_holds_young_rows(scratch_cfg, monkeypatch, capsys):
    cand = _load_script("drop_dead_relationship_candidates")
    _env(monkeypatch, scratch_cfg)
    tag = f"dmg{uuid4().hex[:8]}"
    conn = await asyncpg.connect(scratch_cfg.dsn)
    try:
        await conn.execute("DELETE FROM proposed_edges WHERE status IN ('rejected','retired','orphaned')")
        await _seed(conn, tag)
        await conn.execute(
            "UPDATE proposed_edges SET reviewed_at = now() - interval '90 days' "
            "WHERE source_entity LIKE $1 AND status='rejected'", f"{tag}-%")
        args = cand.build_parser().parse_args(["--min-age-days", "30", "--skip-text-scan"])
        assert await cand.run(args) == 0
        out = capsys.readouterr().out
        assert "HELD BACK (younger than --min-age-days)" in out
    finally:
        await _cleanup(conn, tag)
        await conn.close()


# ---------------------------------------------------------------------------
# The AGE graph dump (item 4)
# ---------------------------------------------------------------------------


def test_age_dump_script_refuses_to_apply_anything_and_is_read_only():
    src = (REPO / "scripts" / "dump_age_graph.py").read_text(encoding="utf-8")
    up = src.upper()
    for w in ("DROP_GRAPH", "DROP SCHEMA", "DELETE FROM", "TRUNCATE", "--APPLY"):
        assert w not in up, w
    assert "default_transaction_read_only" in src


def test_age_dump_manifest_shape_is_json():
    mod = _load_script("dump_age_graph")
    assert json.loads(json.dumps(mod.empty_manifest("db")))["graph"] == "legba_graph"
