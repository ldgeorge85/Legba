# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0194 — the first-pass host `license_class` ratification.

`planning/HOST_LICENSE_CLASSIFICATION_PROPOSAL_2026-09-05.md` §2 (CLEARED,
10 hosts) and §3 (FORBIDDEN, 0 hosts) applied as idempotent, never-clobber
UPDATEs against `source_credibility.license_class` (migration 0192). §4's 24
genuinely-ambiguous hosts and §5's 85-host TEASER bookkeeping stamp are
DELIBERATELY absent — this migration's only job is the 10 §2 hosts.

Everything here runs the migration's REAL SQL text against a real Postgres
connection (never a mock), because the guarantees under test are properties
of the SQL itself: it must be idempotent under a re-glob, it must never widen
a host an operator has already classified some other way, and the exact
classes it writes must be members of the CODE's own vocabularies (not just
this file's belief about them) — a mocked connection would only prove the
Python called the SQL it was written to call.
"""

from __future__ import annotations

import re
from typing import get_args

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic_handlers.evidence_archiver import (
    FORBID_RETENTION_CLASSES,
)
from legba.data.config import PostgresConfig
from legba.data.migrations import MIGRATIONS_DIR
from legba.data.research_evidence import CLEARED_LICENSE_CLASSES
from legba.data.schemas.source import LicenseClass

MIGRATION_NAME = "0194_host_license_first_pass.sql"

# The §2 CLEARED table this migration is supposed to write, host -> class.
EXPECTED_CLEARED = {
    "cdc.gov": "public_domain",
    "eia.gov": "public_domain",
    "federalreserve.gov": "public_domain",
    "nasa.gov": "public_domain",
    "state.gov": "public_domain",
    "usgs.gov": "public_domain",
    "weather.gov": "public_domain",
    "gov.uk": "open_gov_attribution",
    "who.int": "cc_nc",
    "globalvoices.org": "cc_by",
}

# §4 — genuinely-ambiguous hosts the proposal explicitly leaves untouched
# (TEASER/NULL). The migration must never mention any of these.
SECTION_4_HOSTS = [
    "kremlin.ru", "un.org", "wto.org", "iaea.org", "gdacs.org",
    "seismicportal.eu", "pancanal.com", "allafrica.com", "t.me",
    "bbc.com", "bbc.co.uk", "npr.org", "cbc.ca", "abc.net.au",
    "theguardian.com",
    "aljazeera.com", "aljazeera.net",
    "economist.com", "foreignaffairs.com", "foreignpolicy.com", "ft.com",
    "wsj.com", "nytimes.com", "washingtonpost.com",
]
assert len(SECTION_4_HOSTS) == 24, "the proposal's own §4 count"


def _migration_sql() -> str:
    return (MIGRATIONS_DIR / MIGRATION_NAME).read_text(encoding="utf-8")


def _strip_sql_comments(sql: str) -> str:
    """Drop everything from `--` to end-of-line, on every line.

    Good enough for this migration file: no string literal here contains a
    literal `--` (hosts and license class names are plain identifiers), so a
    naive per-line strip cannot mis-cut a real statement.
    """
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    yield c
    await c.close()


# ---------------------------------------------------------------------------
# (b) the vocabulary — every class this migration writes is a member of both
# the closed schema Literal and the code's CLEARED set; none is a member of
# the FORBID set. This is a static check against the .sql TEXT, not the DB —
# it must hold even before any host row exists to update.
# ---------------------------------------------------------------------------


def test_every_written_class_is_in_the_cleared_vocabulary():
    active_sql = _strip_sql_comments(_migration_sql())
    written_classes = set(re.findall(r"license_class\s*=\s*'([a-z_]+)'", active_sql))

    assert written_classes, "expected at least one active UPDATE ... SET license_class = '...'"
    assert written_classes == set(EXPECTED_CLEARED.values())

    schema_vocabulary = set(get_args(LicenseClass))
    for cls in written_classes:
        assert cls in schema_vocabulary, f"{cls!r} is not in schemas.source.LicenseClass"
        assert cls in CLEARED_LICENSE_CLASSES, (
            f"{cls!r} is not in research_evidence.CLEARED_LICENSE_CLASSES — "
            "writing it would not actually clear full_text depth"
        )
        assert cls not in FORBID_RETENTION_CLASSES, (
            f"{cls!r} is in evidence_archiver.FORBID_RETENTION_CLASSES — "
            "this migration must write zero forbidding verdicts (§3 = 0 hosts)"
        )


def test_zero_forbidden_classes_are_written():
    """§3 = 0 qualifying hosts; the FORBIDDEN template must stay commented out."""
    active_sql = _strip_sql_comments(_migration_sql())
    for forbidden_cls in FORBID_RETENTION_CLASSES:
        assert f"license_class = '{forbidden_cls}'" not in active_sql
        assert f"license_class='{forbidden_cls}'" not in active_sql


# ---------------------------------------------------------------------------
# (c) no §4 host appears anywhere in the ACTIVE sql (comments may of course
# discuss them by name — the header does — so this checks the executable
# text only, after comment-stripping).
# ---------------------------------------------------------------------------


def test_no_section4_host_appears_in_the_active_sql():
    active_sql = _strip_sql_comments(_migration_sql())
    for host in SECTION_4_HOSTS:
        assert host not in active_sql, (
            f"{host!r} is a §4 operator-must-decide host and must not be "
            "touched by this migration"
        )


def test_header_cites_the_proposal_and_the_vocabulary_source():
    sql = _migration_sql()
    assert "HOST_LICENSE_CLASSIFICATION_PROPOSAL_2026-09-05" in sql
    assert "CLEARED_LICENSE_CLASSES" in sql
    assert "FORBID_RETENTION_CLASSES" in sql


# ---------------------------------------------------------------------------
# (a) idempotent apply against a real DB, and the never-clobber guard.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_ten_cleared_hosts_stamp_correctly_and_reapply_is_idempotent(
    conn,
):
    sql = _migration_sql()
    try:
        # Seed bare rows for the 10 hosts this migration targets — the fresh
        # test substrate carries none of them (these 10 hosts were added to
        # the live ledger via the source_credibility API, not a seed
        # migration), so the migration's own UPDATEs would otherwise match
        # zero rows and prove nothing.
        for host in EXPECTED_CLEARED:
            await conn.execute(
                "INSERT INTO source_credibility (source_host, score, tier) "
                "VALUES ($1, 0.5, 'gov') ON CONFLICT (source_host) DO NOTHING",
                host,
            )

        # First apply: every seeded row moves off NULL to its expected class.
        await conn.execute(sql)
        rows = await conn.fetch(
            "SELECT source_host, license_class, scored_by FROM source_credibility "
            "WHERE source_host = ANY($1::text[])",
            list(EXPECTED_CLEARED),
        )
        got = {r["source_host"]: r["license_class"] for r in rows}
        assert got == EXPECTED_CLEARED
        assert all(r["scored_by"] == "migration.0194" for r in rows)

        # Second apply (the runner re-globs every file on every run): the
        # WHERE ... license_class IS NULL guard means the second pass matches
        # zero rows — same end state, no error, no duplicate write.
        await conn.execute(sql)
        rows_again = await conn.fetch(
            "SELECT source_host, license_class FROM source_credibility "
            "WHERE source_host = ANY($1::text[])",
            list(EXPECTED_CLEARED),
        )
        assert {r["source_host"]: r["license_class"] for r in rows_again} == (
            EXPECTED_CLEARED
        )
    finally:
        await conn.execute(
            "DELETE FROM source_credibility WHERE source_host = ANY($1::text[])",
            list(EXPECTED_CLEARED),
        )


@pytest.mark.asyncio
async def test_a_later_manual_classification_is_never_overwritten(conn):
    """A host an operator already hand-classified to something else — even a
    class this migration would otherwise write — keeps the operator's own
    verdict. The `license_class IS NULL` guard, not a host allowlist, is what
    makes this migration safe to re-run after ANY manual review.
    """
    host = "cdc.gov"
    sql = _migration_sql()
    try:
        await conn.execute(
            "INSERT INTO source_credibility "
            "(source_host, score, tier, license_class, scored_by) "
            "VALUES ($1, 0.9, 'gov', 'unknown', 'operator.manual_review') "
            "ON CONFLICT (source_host) DO UPDATE SET "
            "license_class = EXCLUDED.license_class, "
            "scored_by = EXCLUDED.scored_by",
            host,
        )

        await conn.execute(sql)

        row = await conn.fetchrow(
            "SELECT license_class, scored_by FROM source_credibility "
            "WHERE source_host = $1",
            host,
        )
        assert row["license_class"] == "unknown"
        assert row["scored_by"] == "operator.manual_review"
    finally:
        await conn.execute(
            "DELETE FROM source_credibility WHERE source_host = $1 "
            "AND scored_by IN ('operator.manual_review', 'migration.0194')",
            host,
        )


@pytest.mark.asyncio
async def test_0194_is_in_the_applied_ledger(conn):
    present = await conn.fetchval(
        "SELECT count(*) FROM legba_data_migrations WHERE name = $1",
        MIGRATION_NAME,
    )
    assert present == 1
