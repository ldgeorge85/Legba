# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Off-chain enforcement for the journal kind (plan §3.5 — the gating test).

"Off the chain entirely" is NOT a free property of a separate table — there is a
concrete leak path (the derived_from lineage fan-out). This test enforces the
direction-asymmetric node decision two ways:

  1. Structural (no DB): `journal_entries` is NOT in the lineage catalog
     (`_SUBSTRATE_TABLES`) and `journal` is NOT a valid walk ROOT
     (`_TABLES_BY_KIND`) — so it can never be returned as a derived child nor
     walked as a root.
  2. DB-level: a journal entry written alongside a fact whose derived_from
     points at a shared parent NEVER appears in the downstream child fan-out
     (`_fetch_children_of`) from any node — because the journal's derived_from is
     ALWAYS empty AND the table is absent from the catalog.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.provenance import (
    AnalystContext,
    FactPayload,
    write_fact,
    write_journal,
)
from legba.data.registry import lineage_api
from legba.data.registry.lineage_api import _SUBSTRATE_TABLES, _TABLES_BY_KIND


# ---------------------------------------------------------------------------
# Structural — the catalog must not know the journal table/kind (no DB)
# ---------------------------------------------------------------------------


def test_journal_table_not_in_lineage_catalog():
    """journal_entries is excluded from the downstream fan-out catalog so a
    derived_from && walk can never surface a journal node (§3.5)."""
    tables = {t.table for t in _SUBSTRATE_TABLES}
    assert "journal_entries" not in tables, (
        "journal_entries must NOT be in _SUBSTRATE_TABLES — the downstream "
        "derived_from fan-out would surface the journal inside the chain (§3.5)"
    )


def test_journal_not_a_valid_lineage_root():
    """`journal` is not a walkable root_kind (it is a direction-asymmetric node;
    the chip walk reads in-payload refs to go UP, never the lineage router)."""
    assert "journal" not in _TABLES_BY_KIND


# ---------------------------------------------------------------------------
# DB-level — the downstream child fan-out never returns a journal row
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()


def _ctx(target_id=None) -> AnalystContext:
    return AnalystContext(
        analyst_id="journal_assessor",
        analyst_version="v" + uuid4().hex[:8],
        run_id=uuid4(),
        target_id=target_id,
        target_version="abc123def456" if target_id else None,
    )


@pytest.mark.asyncio
async def test_downstream_walk_never_returns_journal(pg_conn):
    """Write a fact and a journal entry. The downstream child fan-out from ANY
    candidate parent id must never return the journal row (§3.5 gating test)."""
    now = datetime.now(tz=timezone.utc)

    # A fact (on the chain) derived from a synthetic parent id.
    parent_id = uuid4()
    fact_ctx = _ctx(target_id="br_energy_test")
    fout, _ = await write_fact(
        pg_conn,
        analyst_ctx=fact_ctx,
        payload=FactPayload(
            subject="x", predicate="leads", value="y", valid_from=now
        ),
        derived_from=[parent_id],
    )
    assert fout is not None

    # A journal entry that TRIES to cite the same parent + the fact — but the
    # write path forces derived_from empty (off-chain).
    jout, _ = await write_journal(
        pg_conn,
        analyst_ctx=_ctx(),
        payload={
            "entry_kind": "entry",
            "title": "t",
            "body": "reflecting [[ref:%s]]" % fout.id,
            "cited_substrate_refs": [fout.id],
            "period_start": now,
            "period_end": now,
        },
        derived_from=[parent_id, fout.id],  # IGNORED — off-chain
    )
    assert jout is not None

    # The journal row's derived_from is empty in the DB.
    j_derived = await pg_conn.fetchval(
        "SELECT derived_from FROM journal_entries WHERE id=$1", jout.id
    )
    assert list(j_derived) == []

    # Downstream fan-out from the shared parent AND from the fact must never
    # surface the journal row — it isn't in the catalog and its derived_from is
    # empty, so neither path can reach it.
    for probe in (parent_id, fout.id):
        children = await lineage_api._fetch_children_of(pg_conn, [probe])
        ids = {str(r["id"]) for r in children}
        assert str(jout.id) not in ids, (
            f"journal row leaked into the downstream walk from {probe} (§3.5)"
        )
