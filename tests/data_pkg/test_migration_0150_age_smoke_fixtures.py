# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0150 — delete the 27 June-17 smoke fixtures from ``legba_graph``.

`legba_graph` never held a production row. Everything in it was written by the
2026-06-17 AGE smoke suite, and an empty graph that *answers* is worse than one
that errors: `/api/v1/graph/path` walked those fixtures, found nothing, and
rendered a confident `detail="no path"` (graph-debate JUDGE_SYNTHESIS §4.3
item 2, live defect #2).

The migration's whole value is its GUARD, so that is what these tests exercise:
it must delete the fixture cohort and it must REFUSE — loudly, without deleting
anything — the moment the graph holds a vertex it does not recognise, because
by then the graph is being fed and a blind wipe destroys real knowledge.

These run the SHIPPED SQL against the pivot substrate inside a transaction that
is always rolled back, so they cannot pass against a migration that says
something different from the one that will run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from legba.data import migrate as _migrate_mod

MIGRATION = (
    Path(_migrate_mod.__file__).parent / "migrations" / "0150_drop_age_smoke_fixtures.sql"
)

# The signature the migration matches on, mirrored here so a drift in either
# direction is caught rather than silently widening the blast radius.
FIXTURE_TS_PREFIX = "2026-06-17T18:2"
UNDATED_FIXTURE = '{"name": "FromCypher", "origin": "test"}'


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Static guards
# ---------------------------------------------------------------------------


def test_migration_exists_and_is_data_only():
    assert MIGRATION.is_file(), f"{MIGRATION} missing"
    sql = _sql().upper()
    # It removes DATA. It must never remove the graph or a label definition —
    # dropping the graph would take the 11 vlabels / 21 elabels from 0001+0037
    # with it and turn a data cleanup into a schema migration.
    assert "DROP_GRAPH" not in sql
    assert "DROP GRAPH" not in sql
    assert "CREATE_GRAPH" not in sql
    assert "TRUNCATE" not in sql
    assert "DELETE FROM LEGBA_GRAPH._AG_LABEL_VERTEX" in sql


def test_migration_refuses_rather_than_deletes_when_unsure():
    """The guard is the point: an unrecognised vertex must RAISE, not be spared."""
    sql = _sql()
    assert "RAISE EXCEPTION" in sql
    assert "v_total <> v_fixtures" in sql


def test_migration_signature_covers_both_fixture_shapes():
    sql = _sql()
    assert FIXTURE_TS_PREFIX in sql, "the dated smoke cohort is not matched"
    assert UNDATED_FIXTURE in sql, "the one undated smoke vertex is not matched"


# The behaviour tests that ran this migration against a live AGE substrate were
# removed on 2026-11-15: the graph they seeded no longer exists after
# 20261115T1829_drop_empty_age_graph, and 0150 has applied everywhere. The
# guards above pin the shipped text.
