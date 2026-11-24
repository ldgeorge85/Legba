# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every table names its writer class in ONE place (data-model lane 9).

``legba.data.write_attribution.TABLES`` classifies every table the migrations
(and the declared runtime DDL) create as (a) already names its writer, (b)
carries ``written_by``, or (c) a machine table whose writer is implied. These
tests fail on:

* a table a migration creates that is not classified (a new table cannot land
  unclassified — the planted-table test proves the check bites);
* a classified name no migration creates (a stale entry);
* a (b) table the migrations never give a ``written_by`` column;
* a module that writes a (b) table without filling ``written_by`` (unless the
  entry names it as bookkeeping);
* a (c) writer that is not a well-formed principal handle of a declared kind.

Pure source reads: no database.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest

from legba.data import principals
from legba.data.migrations import MIGRATIONS_DIR, ordered_migration_files
from legba.data.schema_hygiene import RUNTIME_CREATED, declared_tables, strip_comments
from legba.data.write_attribution import CLASS_A, CLASS_B, CLASS_C, TABLES, by_class

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "legba"

#: Runtime-created names that are not tables of their own (indexes, and the
#: graph projector's staging/retired tables that exist only during a swap).
_RUNTIME_NOT_TABLES = ("*_idx", "graph_arcs_new", "graph_arcs_old", "graph_arcs_new_*")


def _migrations() -> list[tuple[str, str]]:
    return [(p.name, p.read_text(encoding="utf-8")) for p in ordered_migration_files(MIGRATIONS_DIR)]


def _expected_tables(migrations: list[tuple[str, str]]) -> set[str]:
    names = set(declared_tables(migrations))
    for name in RUNTIME_CREATED:
        if not any(fnmatch.fnmatchcase(name, pat) for pat in _RUNTIME_NOT_TABLES):
            names.add(name)
    return names


def _unclassified(expected: set[str]) -> list[str]:
    return sorted(
        n for n in expected
        if n not in TABLES and not any(
            "*" in k and fnmatch.fnmatchcase(n, k) for k in TABLES)
    )


def test_every_declared_table_is_classified():
    missing = _unclassified(_expected_tables(_migrations()))
    assert not missing, (
        "classify each new table in src/legba/data/write_attribution.py as "
        f"(a) already names its writer, (b) written_by, or (c) implied writer: {missing}")


def test_a_planted_table_is_caught():
    planted = _migrations() + [("29991231T2359_planted.sql", "CREATE TABLE public.planted_unclassified (id int);")]
    assert _unclassified(_expected_tables(planted)) == ["planted_unclassified"]


def test_no_classified_name_is_stale():
    expected = _expected_tables(_migrations())
    stale = sorted(
        k for k in TABLES
        if k not in expected and not ("*" in k and any(fnmatch.fnmatchcase(n, k) for n in expected))
    )
    assert not stale, f"classified tables no migration creates: {stale}"


def test_every_entry_is_well_formed():
    for name, a in TABLES.items():
        assert a.cls in (CLASS_A, CLASS_B, CLASS_C), name
        if a.cls == CLASS_A:
            assert a.columns and all(a.columns), name
        if a.cls == CLASS_C:
            assert a.writer, name
            principals.validate_handle(a.writer)  # raises on an undeclared kind
        if a.cls != CLASS_B:
            assert not a.bookkeeping, name
        for path in a.bookkeeping:
            assert (REPO / path).is_file(), f"{name}: bookkeeping module {path} does not exist"


def _written_by_tables(migrations: list[tuple[str, str]]) -> set[str]:
    out: set[str] = set()
    for _f, text in migrations:
        sql = strip_comments(text)
        for m in re.finditer(
            r"ALTER\s+TABLE\s+(?:public\.)?(\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?written_by\b",
            sql, re.I,
        ):
            out.add(m.group(1))
        for m in re.finditer(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:public\.)?(\w+)\s*\((.*?)\n\);", sql, re.I | re.S):
            if re.search(r"^\s*written_by\s+uuid", m.group(2), re.M):
                out.add(m.group(1))
    return out


def test_every_class_b_table_has_a_written_by_column():
    have = _written_by_tables(_migrations())
    missing = sorted(set(by_class(CLASS_B)) - have)
    assert not missing, f"class (b) tables without a written_by column: {missing}"


def test_written_by_is_only_on_class_b():
    extra = sorted(_written_by_tables(_migrations()) - set(by_class(CLASS_B)))
    assert not extra, f"written_by columns on tables not classified (b): {extra}"


def _writer_modules(table: str) -> list[Path]:
    # SQL keywords in capitals, as the tree writes them (a prose "insert into"
    # in a comment is not a write).
    rx = re.compile(rf"\b(?:INSERT\s+INTO|UPDATE)\s+(?:public\.)?{table}\b")
    return sorted(p for p in SRC.rglob("*.py") if rx.search(p.read_text(encoding="utf-8")))


@pytest.mark.parametrize("table", sorted(by_class(CLASS_B)))
def test_every_writer_of_a_class_b_table_fills_written_by(table):
    a = TABLES[table]
    offenders = []
    for path in _writer_modules(table):
        rel = str(path.relative_to(REPO))
        if rel in a.bookkeeping:
            continue
        if "written_by" not in path.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert not offenders, (
        f"{table}: these modules write it without filling written_by "
        f"(fill it, or name the module as bookkeeping in write_attribution): {offenders}")


def test_bookkeeping_modules_really_write_the_table():
    for name, a in by_class(CLASS_B).items():
        writers = {str(p.relative_to(REPO)) for p in _writer_modules(name)}
        for path in a.bookkeeping:
            assert path in writers, f"{name}: {path} is declared bookkeeping but writes nothing to it"


def test_the_classification_counts_are_reported():
    """Not a pin: the three classes are non-empty and add up."""
    counts = {c: len(by_class(c)) for c in (CLASS_A, CLASS_B, CLASS_C)}
    assert all(counts.values())
    assert sum(counts.values()) == len(TABLES)


def test_the_written_by_report_has_a_query_for_every_class_b_table():
    """``scripts/report_written_by.py`` refuses to run (exit 1) when a class
    (b) table has no entry in its QUERIES; a table added to class (b) must
    get one (``None`` when the table is newer than the column)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_report_written_by", REPO / "scripts" / "report_written_by.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    missing = sorted(set(by_class(CLASS_B)) - set(mod.QUERIES))
    assert not missing, f"no report query for class (b) tables: {missing}"
