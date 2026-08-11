# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0176 (capital-as-government metonymy repair) must stay in lockstep
with the LIVE guard it is the historical half of.

The repair's whole claim to safety is that its SQL predicate IS
``fact_extractor._is_capital_metonymy``'s predicate — narrow on both axes
(containment/location predicates absent so a city keeps its real facts,
city-states absent from the subjects so Singapore keeps its treaties). If the
Python sets and the SQL lists drift, that claim quietly stops being true in
whichever direction the edit went: a widened guard leaves a cohort the migration
never closed, and a widened migration closes facts the guard would have kept.

Also pins the second half of the fix: the contention arbiter reuses the
extractor's junk gates, and a gate missing from that chain is a class that
re-forms in the contention plane forever — which is exactly what happened to
capital metonymy until 2026-08-03.
"""

from __future__ import annotations

import re
from pathlib import Path

from legba.data.filters.fact_extractor import (
    _GOVERNMENT_METONYM_SUBJECTS,
    _STATE_ONLY_PREDICATES,
    _is_capital_metonymy,
)
from legba.data.vocabulary import PREDICATE_CANONICAL

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src/legba/data/migrations/0176_soft_close_capital_metonymy_facts.sql"
)


def _sql_values(sql: str, table: str) -> set[str]:
    """The literals INSERTed into one temp table of the migration."""
    start = sql.index(f"INSERT INTO {table}")
    end = sql.index(";", start)
    body = sql[start:end]
    # SQL doubles a quote to escape it ("n''djamena").
    return {m.group(1).replace("''", "'") for m in re.finditer(r"\('([^)]*?)'\)", body)}


def test_migration_file_exists() -> None:
    assert MIGRATION.is_file(), f"missing {MIGRATION}"


def test_metonymy_repair_matches_the_live_guard() -> None:
    """The SQL subject list IS ``_GOVERNMENT_METONYM_SUBJECTS``."""
    sql = MIGRATION.read_text(encoding="utf-8")
    assert _sql_values(sql, "_metonymy_subjects") == set(_GOVERNMENT_METONYM_SUBJECTS)


def test_metonymy_repair_predicates_cover_the_guard_and_its_canon_folds() -> None:
    """The SQL predicate list is the guard's set PLUS every CamelCase key that
    ``normalize_predicate`` folds into it.

    The guard normalizes before testing; a stored row may carry either surface,
    so the SQL must accept both or it silently misses the seed driver's spelling.
    """
    sql = MIGRATION.read_text(encoding="utf-8")
    folded = {k for k, v in PREDICATE_CANONICAL.items() if v in _STATE_ONLY_PREDICATES}
    assert _sql_values(sql, "_metonymy_predicates") == (
        set(_STATE_ONLY_PREDICATES) | folded
    )


def test_the_guard_still_spares_a_citys_real_facts() -> None:
    """The narrowness the migration's safety argument rests on."""
    assert not _is_capital_metonymy("Madrid", "located in", "Spain")
    assert not _is_capital_metonymy("Madrid", "capital of", "Spain")
    assert not _is_capital_metonymy("Paris", "part of", "France")
    # City-states: the city IS the state, so its inter-state relations are real.
    assert not _is_capital_metonymy("Singapore", "member of", "ASEAN")
    assert not _is_capital_metonymy("Vatican City", "diplomatic relations with", "Italy")
    # The class the repair closes.
    assert _is_capital_metonymy("Madrid", "border with", "France")
    assert _is_capital_metonymy("Washington", "member of", "NATO")
    assert _is_capital_metonymy("Kiev", "conflict with", "Russia")


def test_the_arbiter_junk_gate_carries_the_metonymy_check() -> None:
    """Without this the class re-forms in the contention plane after 0176."""
    from legba.data.analysts.deterministic_handlers.fact_contention_arbiter import (
        _junk_reason,
    )

    assert _junk_reason("Madrid", "border with", "France") == "capital_metonymy"
    assert _junk_reason("Washington", "member of", "NATO") == "capital_metonymy"
    # Still None for a city's genuine fact.
    assert _junk_reason("Madrid", "capital of", "Spain") is None


def test_migration_soft_closes_and_never_deletes() -> None:
    """The 0117 posture: provenance is preserved, the repair is reversible."""
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "delete from facts" not in sql
    assert "valid_until = now()" in sql
    assert "mig_0176_capital_metonymy" in sql


def test_migration_guards_group_collapse_on_no_surviving_member() -> None:
    """A contention group holding a REAL fact must stay open."""
    sql = MIGRATION.read_text(encoding="utf-8")
    collapse = sql[sql.index("UPDATE fact_contention c") :]
    assert "NOT EXISTS" in collapse
    assert "valid_until IS NULL" in collapse
