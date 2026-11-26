# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Schema hygiene: every table, index and view in the
application schema is created by a migration (or named, with its reason, in
``schema_hygiene.RUNTIME_CREATED``); migration 0264's index drops and the
out-of-transaction script that finishes (and reverses) them agree.

The pure tests read files only. The two ``migrated_pg`` tests read the
migrated test database's catalog: the parser and the migrations agree in both
directions, and a planted table is caught.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from legba.data import schema_hygiene as sh
from legba.data.migrations import MIGRATIONS_DIR

REPO = Path(__file__).resolve().parents[2]


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod  # dataclasses resolve their module by name
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- the parser


def test_parser_follows_create_rename_drop_in_order():
    declared = sh.declared_objects([
        ("0001_a.sql", """
            -- CREATE TABLE commented_out (x int);
            CREATE TABLE IF NOT EXISTS public.alpha (id int);
            CREATE UNIQUE INDEX IF NOT EXISTS alpha_id ON public.alpha (id);
            CREATE INDEX beta_idx ON alpha (id);
            CREATE OR REPLACE VIEW v_alpha AS SELECT 1;
            CREATE TABLE ag_catalog.not_ours (id int);
        """),
        ("0002_b.sql", """
            ALTER TABLE alpha RENAME TO gamma;
            DROP INDEX IF EXISTS public.beta_idx;
            DO $$ BEGIN EXECUTE format('CREATE TABLE IF NOT EXISTS public.obs_%s '
                'PARTITION OF public.obs FOR VALUES FROM (1) TO (2)', 2020); END $$;
        """),
        ("0003_c.sql", "DROP TABLE gamma CASCADE;"),
    ])
    assert "commented_out" not in declared
    assert "not_ours" not in declared
    assert "alpha" not in declared and "gamma" not in declared
    # the table's drop took its surviving index with it
    assert "alpha_id" not in declared
    assert "beta_idx" not in declared
    assert declared["v_alpha"] == "0001_a.sql"
    assert declared["obs_*"] == "0002_b.sql"


def test_undeclared_covers_constraint_indexes_and_runtime_names():
    cat = [
        sh.CatalogObject("alpha", "table", None, 8192, False, False),
        sh.CatalogObject("alpha_pkey", "index", "alpha", 8192, True, False),
        sh.CatalogObject("actor_state", "table", None, 8192, False, False),
        sh.CatalogObject("graph_arcs_new_from_id_idx", "index", "graph_arcs", 1, False, False),
        sh.CatalogObject("planted_bak_20261114", "table", None, 16384, False, False),
        sh.CatalogObject("planted_bak_pkey", "index", "planted_bak_20261114", 1, True, False),
    ]
    names = [o.name for o in sh.undeclared(cat, {"alpha": "0001_a.sql"})]
    assert names == ["planted_bak_20261114", "planted_bak_pkey"]


@pytest.mark.parametrize("name", sorted(sh.RUNTIME_CREATED))
def test_runtime_created_names_are_really_created_by_their_file(name):
    path, reason = sh.RUNTIME_CREATED[name]
    assert reason
    text = (REPO / path).read_text(encoding="utf-8")
    if "*" in name and name.startswith("graph_arcs_new_"):
        # Postgres names the staging table's indexes. Since then the
        # LIKE copy takes no index; the primary key and the post-load indexes
        # are created on graph_arcs_new without a name.
        assert "LIKE public.graph_arcs" in text
        assert "ADD PRIMARY KEY" in text and "CREATE INDEX ON public.graph_arcs_new" in text
        return
    rx = re.escape(name).replace(r"\*", r"[a-z_]+")
    assert re.search(
        rf"(?:CREATE (?:TABLE|INDEX)(?: IF NOT EXISTS)? (?:public\.)?|RENAME TO ){rx}\b",
        text,
    ), f"{path} no longer creates {name}"


# ------------------------------------------------- 0264 and its live script


def _mig_0264() -> str:
    return (MIGRATIONS_DIR / "0264_drop_unused_indexes.sql").read_text(encoding="utf-8")


def test_0264_drop_list_equals_the_script_list():
    dropped = set(re.findall(r"DROP INDEX IF EXISTS public\.(\w+);", _mig_0264()))
    script = _load_script("drop_unused_indexes")
    assert dropped == set(script.INDEXES)
    assert len(dropped) == 32


def test_0264_indexes_were_each_created_by_an_earlier_migration_on_their_table():
    texts = [t for t in sh.migration_texts(MIGRATIONS_DIR) if t[0] < "0264"]
    declared = sh.declared_objects(texts)
    script = _load_script("drop_unused_indexes")
    for name, (table, definition) in script.INDEXES.items():
        assert name in declared, f"{name}: no earlier migration creates it"
        assert definition.startswith(f"ON public.{table} USING "), name
    # and after 0264 the parser no longer counts them as declared
    after = sh.declared_objects(sh.migration_texts(MIGRATIONS_DIR))
    assert not set(script.INDEXES) & set(after)


def test_0264_guards_each_drop_by_table_size_and_never_concurrently():
    sql = sh.strip_comments(_mig_0264())
    assert "CONCURRENTLY" not in sql
    assert not re.search(r"^\s*(BEGIN|COMMIT)\s*;", sql, re.M | re.I)
    script = _load_script("drop_unused_indexes")
    blocks = re.findall(
        r"FROM public\.(\w+) LIMIT 50001\) s\) <= 50000 THEN(.*?)(?:ELSE|END IF)",
        sql, re.S)
    guarded = {
        idx: table for table, body in blocks
        for idx in re.findall(r"DROP INDEX IF EXISTS public\.(\w+);", body)
    }
    assert guarded == {n: t for n, (t, _d) in script.INDEXES.items()}


def test_unused_index_classifier_keeps_what_it_must():
    rep = _load_script("report_unused_indexes")
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    since = now - timedelta(days=77)

    def row(name, table="t", **kw):
        base = {"name": name, "table_name": table, "idx_scan": 0, "bytes": 8192,
                "is_unique": False, "backs_constraint": False,
                "definition": f"CREATE INDEX {name} ON public.{table} (x)",
                "table_rows": 10}
        base.update(kw)
        return base

    rows = [
        row("used", idx_scan=3),
        row("uniq", is_unique=True),
        row("arc", table="graph_arcs"),
        row("fact_idx", table="facts"),
        row("young"),
        row("hinted"),
        row("pinned"),
        row("on_empty", table_rows=0),
        row("handmade"),
        row("dead"),
    ]
    declared = {n: "0001_baseline.sql" for n in
                ("uniq", "arc", "fact_idx", "hinted", "pinned", "on_empty", "dead")}
    declared["young"] = "0300_new.sql"
    out = {v.name: v for v in rep.classify(
        rows, declared=declared,
        applied_at={"0300_new.sql": now - timedelta(days=3)},
        stats_since=since, now=now, min_days=30,
        refs={"hinted": ["src/x.py"]}, pins={"pinned": ["tests/t.py"]},
    )}
    assert "used" not in out
    assert out["dead"].verdict == "drop-candidate"
    assert all(v.verdict == "keep" for n, v in out.items() if n != "dead")
    assert "unique" in out["uniq"].reason
    assert "swap" in out["arc"].reason
    assert "owner-held" in out["fact_idx"].reason
    assert "observed only 3 d" in out["young"].reason
    assert "named in code" in out["hinted"].reason
    assert "pinned" in out["pinned"].reason
    assert "empty" in out["on_empty"].reason
    assert "not created by a migration" in out["handmade"].reason


def test_backup_archive_script_is_dry_run_and_never_drops_on_an_old_dump():
    arch = _load_script("archive_backup_tables")
    assert len(arch.BACKUP_TABLES) == 9
    assert all(("_bak_" in t or "_backup_" in t) for t in arch.BACKUP_TABLES)
    src = (REPO / "scripts" / "archive_backup_tables.py").read_text(encoding="utf-8")
    # A drop needs a dump in the SAME invocation.
    assert "if args.apply and args.dump is None" in src
    assert "if args.dump is None:\n        return await dry_run(args)" in src


# ------------------------------------------- against the migrated test DB


@pytest.mark.asyncio
async def test_migrated_schema_holds_exactly_what_the_migrations_declare(migrated_pg):
    import asyncpg

    declared = sh.declared_objects(sh.migration_texts(MIGRATIONS_DIR))
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        catalog = await sh.fetch_catalog(conn)
    finally:
        await conn.close()
    extra = sh.undeclared(catalog, declared)
    assert not extra, (
        "objects in the migrated schema that no migration creates "
        "(a parser miss, or DDL outside the migrations): "
        + ", ".join(f"{o.kind} {o.name}" for o in extra))
    missing = sh.declared_but_missing(catalog, declared)
    assert not missing, f"declared by a migration but absent: {missing}"
    script = _load_script("drop_unused_indexes")
    assert not {o.name for o in catalog} & set(script.INDEXES), \
        "0264 should have dropped its indexes on a small database"


@pytest.mark.asyncio
async def test_a_planted_table_is_caught(migrated_pg):
    import asyncpg

    declared = sh.declared_objects(sh.migration_texts(MIGRATIONS_DIR))
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(
                "CREATE TABLE public.p8_planted_bak_20261114 (id int PRIMARY KEY)")
            await conn.execute("CREATE SCHEMA IF NOT EXISTS scratch")
            await conn.execute("CREATE TABLE scratch.p8_ok_here (id int)")
            catalog = await sh.fetch_catalog(conn)
        finally:
            await tx.rollback()
    finally:
        await conn.close()
    names = {o.name for o in sh.undeclared(catalog, declared)}
    assert {"p8_planted_bak_20261114", "p8_planted_bak_20261114_pkey"} <= names
    assert "p8_ok_here" not in names
