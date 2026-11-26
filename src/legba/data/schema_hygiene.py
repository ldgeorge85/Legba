# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Schema hygiene — what the migrations create, and what a database holds
that no migration creates.

Two readings of one question, "is every table, index and view in the
application schema created by a migration?":

  * :func:`declared_objects` reads the migration files (text only, in order)
    and returns the set of relation names they leave behind: every ``CREATE
    TABLE / INDEX / VIEW / MATERIALIZED VIEW``, then ``ALTER … RENAME TO`` and
    ``DROP …`` applied in file order. A name built inside a ``DO`` block with
    ``format('… public.observations_%s …')`` becomes a pattern
    (``observations_*``).
  * :func:`fetch_catalog` reads ``pg_class`` for the application schema
    (``public``) — one row per table, partitioned table, view, materialized
    view and index, with its size and whether it backs a constraint.

:func:`undeclared` joins them: the live objects no migration names. An index
that backs a constraint (``…_pkey``, ``…_key``) or is the child of a
partitioned index is covered by its declared table. Objects in the declared
``scratch`` schema are out of scope by construction (a hand-made object belongs
there). ``RUNTIME_CREATED`` names the few relations code (not a migration)
creates on purpose, each with the reason.

The two readers are ``tests/data_pkg/test_schema_hygiene.py`` (against the
migrated test database: the parser and the migrations agree, and a planted
table is caught) and ``scripts/report_schema_drift.py`` (read-only against
live: the list of objects no migration creates, with sizes).

Stdlib only, plus the caller's asyncpg connection; never writes.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

#: The application schema the guard covers.
APP_SCHEMA = "public"

#: The one schema where hand-made objects (repair backups, one-off
#: comparisons) may live without a migration. Out of the guard's scope.
SCRATCH_SCHEMA = "scratch"

#: Relations CODE creates on purpose (not a migration): name (glob allowed)
#: → (the repo file whose DDL creates it, the reason). A name here is not
#: reported; the test checks each file still carries a CREATE for it. Keep
#: this list short: anything durable belongs in a migration (the four
#: startup-DDL tables are a standing follow-up, not a precedent).
RUNTIME_CREATED: dict[str, tuple[str, str]] = {
    "legba_data_migrations": (
        "src/legba/data/migrate.py",
        "the migration runner's own ledger, created before the first migration",
    ),
    "actor_state": (
        "src/legba/runtime/state.py",
        "runtime actor state; startup DDL (CREATE TABLE IF NOT EXISTS)",
    ),
    "actor_state_*_idx": (
        "src/legba/runtime/state.py", "actor_state's startup-DDL indexes",
    ),
    "actor_filter_state": (
        "src/legba/runtime/state.py",
        "per-pipeline filter state; startup DDL (CREATE TABLE IF NOT EXISTS)",
    ),
    "legba_jobs": (
        "src/legba/data/jobs/store.py",
        "job store; startup DDL (CREATE TABLE IF NOT EXISTS)",
    ),
    "legba_jobs_*_idx": (
        "src/legba/data/jobs/store.py", "legba_jobs' startup-DDL indexes",
    ),
    # The graph projector rebuilds `graph_arcs` hourly into a staging
    # `graph_arcs_new` (since P-7: LIKE … without indexes, the primary key and
    # two indexes created unnamed after the load) and swaps it in by RENAME.
    # Those indexes keep Postgres's generated names
    # (`graph_arcs_new_<cols>_idx`) after the swap, so on a database the
    # projector has run on, 0207's `idx_graph_arcs_*` names are replaced by
    # these (see SWAP_RENAMED). The staging/retired tables exist only for
    # the length of one rebuild (0210 sweeps a leftover).
    "graph_arcs_new": (
        "src/legba/data/analysts/deterministic_handlers/graph_projector.py",
        "graph projection staging table (exists only during a rebuild)",
    ),
    "graph_arcs_old": (
        "src/legba/data/analysts/deterministic_handlers/graph_projector.py",
        "graph projection retired table (exists only during a swap)",
    ),
    "graph_arcs_new_*": (
        "src/legba/data/analysts/deterministic_handlers/graph_projector.py",
        "graph_arcs' indexes, created unnamed on the staging table at each rebuild",
    ),
}

#: Declared names a database may legitimately NOT hold, with the reason.
SWAP_RENAMED: dict[str, str] = {
    "idx_graph_arcs_*": (
        "0207's graph_arcs indexes; replaced by graph_arcs_new_* generated "
        "names after the projector's first swap"
    ),
}

_REL_KIND = {
    "r": "table", "p": "table", "v": "view", "m": "view",
    "i": "index", "I": "index",
}

_IDENT = r'(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$%]*)'
_QNAME = rf"(?:{_IDENT}\.)?{_IDENT}"

_CREATE_TABLE = re.compile(
    rf"\bCREATE\s+(?:(?:GLOBAL|LOCAL)\s+)?(?:UNLOGGED\s+)?TABLE\s+"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?({_QNAME})",
    re.I,
)
_CREATE_INDEX = re.compile(
    rf"\bCREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?({_QNAME})\s+ON\s+(?:ONLY\s+)?({_QNAME})",
    re.I,
)
_CREATE_VIEW = re.compile(
    rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:MATERIALIZED\s+)?VIEW\s+"
    rf"(?:IF\s+NOT\s+EXISTS\s+)?({_QNAME})",
    re.I,
)
_RENAME = re.compile(
    rf"\bALTER\s+(TABLE|INDEX|VIEW|MATERIALIZED\s+VIEW)\s+(?:IF\s+EXISTS\s+)?"
    rf"(?:ONLY\s+)?({_QNAME})\s+RENAME\s+TO\s+({_IDENT})",
    re.I,
)
_DROP = re.compile(
    r"\bDROP\s+(TABLE|INDEX|VIEW|MATERIALIZED\s+VIEW)\s+(?:CONCURRENTLY\s+)?"
    r"(?:IF\s+EXISTS\s+)?([^;]+?)(?:\s+(?:CASCADE|RESTRICT))?\s*(?:;|$)",
    re.I | re.M,
)
_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _bare(qname: str) -> tuple[str | None, str]:
    """``schema.name`` → (schema | None, name), quotes removed. A
    ``format()`` placeholder (``%s`` / ``%I``) becomes ``*``."""
    parts = [p.strip('"') for p in re.split(r"\.(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", qname)]
    schema = parts[0] if len(parts) == 2 else None
    name = re.sub(r"%[sIL]", "*", parts[-1])
    return schema, name


def _in_app_schema(schema: str | None) -> bool:
    return schema is None or schema == APP_SCHEMA


def strip_comments(sql: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", sql))


def declared_objects(migrations: Iterable[tuple[str, str]]) -> dict[str, str]:
    """``{name_or_glob: migration file}`` for every relation the migrations
    (``(file name, sql text)`` pairs, applied in the order given) leave in the
    application schema."""
    out: dict[str, str] = {}
    index_table: dict[str, str] = {}
    for fname, text in migrations:
        sql = strip_comments(text)
        events: list[tuple[int, str, tuple[str, ...]]] = []
        for rx in (_CREATE_TABLE, _CREATE_VIEW):
            for m in rx.finditer(sql):
                events.append((m.start(), "create", (m.group(1),)))
        for m in _CREATE_INDEX.finditer(sql):
            events.append((m.start(), "create", (m.group(1), m.group(2))))
        for m in _RENAME.finditer(sql):
            events.append((m.start(), "rename", (m.group(2), m.group(3))))
        for m in _DROP.finditer(sql):
            events.append((m.start(), "drop", tuple(
                n.strip() for n in m.group(2).split(",") if n.strip()
            )))
        for _pos, op, args in sorted(events):
            if op == "create":
                schema, name = _bare(args[0])
                if _in_app_schema(schema):
                    out[name] = fname
                    if len(args) == 2:
                        index_table[name] = _bare(args[1])[1]
            elif op == "rename":
                schema, old = _bare(args[0])
                _s, new = _bare(args[1])
                if _in_app_schema(schema) and old in out:
                    out[new] = fname
                    del out[old]
                    for idx, tbl in list(index_table.items()):
                        if tbl == old:
                            index_table[idx] = new
            else:
                for qn in args:
                    schema, name = _bare(qn)
                    if _in_app_schema(schema):
                        out.pop(name, None)
                        # A dropped table takes its indexes with it.
                        for idx, tbl in list(index_table.items()):
                            if tbl == name:
                                out.pop(idx, None)
                                del index_table[idx]
    return out


def declared_tables(migrations: Iterable[tuple[str, str]]) -> dict[str, str]:
    """The TABLES among :func:`declared_objects` (no index, no view):
    ``{name_or_glob: migration file}`` — what ``write_attribution`` must
    classify."""
    migrations = list(migrations)
    created_as_table: set[str] = set()
    for _fname, text in migrations:
        sql = strip_comments(text)
        for m in _CREATE_TABLE.finditer(sql):
            schema, name = _bare(m.group(1))
            if _in_app_schema(schema):
                created_as_table.add(name)
        for m in _RENAME.finditer(sql):
            if m.group(1).upper() == "TABLE":
                created_as_table.add(_bare(m.group(3))[1])
    return {n: f for n, f in declared_objects(migrations).items() if n in created_as_table}


def migration_texts(migrations_dir: Path) -> list[tuple[str, str]]:
    """The numbered migration files, in the runner's order."""
    return [
        (p.name, p.read_text(encoding="utf-8"))
        for p in sorted(migrations_dir.glob("*.sql"))
        if p.stem[:1].isdigit()
    ]


@dataclass(frozen=True)
class CatalogObject:
    name: str
    kind: str              # table | view | index
    table: str | None      # the index's table
    bytes: int
    backs_constraint: bool  # an index behind a PK / UNIQUE / EXCLUDE
    partition_child: bool  # a partition, or an index inherited from a parent


CATALOG_SQL = """
SELECT c.relname AS name, c.relkind::text AS relkind,
       t.relname AS table_name,
       pg_total_relation_size(c.oid) AS bytes,
       EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = c.oid
                 AND k.contype IN ('p','u','x')) AS backs_constraint,
       (c.relispartition
        OR EXISTS (SELECT 1 FROM pg_inherits h WHERE h.inhrelid = c.oid))
           AS partition_child
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_index x ON x.indexrelid = c.oid
  LEFT JOIN pg_class t ON t.oid = x.indrelid
 WHERE n.nspname = $1
   AND c.relkind IN ('r','p','v','m','i','I')
 ORDER BY c.relname
"""


async def fetch_catalog(conn: Any, schema: str = APP_SCHEMA) -> list[CatalogObject]:
    rows = await conn.fetch(CATALOG_SQL, schema)
    return [
        CatalogObject(
            name=r["name"], kind=_REL_KIND[r["relkind"]], table=r["table_name"],
            bytes=int(r["bytes"] or 0), backs_constraint=bool(r["backs_constraint"]),
            partition_child=bool(r["partition_child"]),
        )
        for r in rows
    ]


def _matches(name: str, names: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pat) for pat in names)


def undeclared(
    catalog: Iterable[CatalogObject],
    declared: dict[str, str],
    *,
    runtime_created: dict[str, tuple[str, str]] = RUNTIME_CREATED,
) -> list[CatalogObject]:
    """The catalog objects no migration creates (and no ``RUNTIME_CREATED``
    entry names). A constraint-backed index or a partition child is covered
    when its table is."""
    catalog = list(catalog)
    known = set(declared) | set(runtime_created)
    tables_ok = {
        o.name for o in catalog if o.kind != "index" and _matches(o.name, known)
    }
    out: list[CatalogObject] = []
    for o in catalog:
        if _matches(o.name, known):
            continue
        if o.kind == "index" and (o.backs_constraint or o.partition_child) \
                and o.table in tables_ok:
            continue
        if o.kind == "table" and o.partition_child:
            # A partition built by a loop the parser could not expand is
            # still reported: name it in the migration or RUNTIME_CREATED.
            pass
        out.append(o)
    return out


def declared_but_missing(
    catalog: Iterable[CatalogObject], declared: dict[str, str],
) -> list[str]:
    """Declared (non-glob) names the database does not hold — the other
    direction of drift (a parser miss, or a migration that did not apply)."""
    have = {o.name for o in catalog}
    return sorted(
        n for n in declared
        if "*" not in n and n not in have and not _matches(n, SWAP_RENAMED)
    )
