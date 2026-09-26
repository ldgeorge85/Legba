# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for V3/P7 — the origin-class columns and the reader firewall
(migration 0209, ``data/provenance/origin.py``).

Covers the load-bearing claims:

* a plain fact insert reads back ``live``; a seed batch through the driver
  reads back ``seed``; a research-evidence signal reads back
  ``web_retrieval`` and :func:`origin_class_for` agrees with the stamp;
* a write naming a history class fails AT THE TABLE, and the error names
  ``facts_origin_class_history_writer_not_built`` — renamed from
  ``readers_not_swept`` by migration 0220 when the READER sweep closed
  (SEAMS #57) and the guard's remaining job became "no history WRITER exists
  for this table yet" (SEAMS #62);
* a supersession stamps ``superseded_at`` (migration 0209 closes the
  asymmetry ``analyst_outputs`` already had);
* a row transactionally forced to ``archive`` is absent from the port's
  open read and present with ``include_origin=['archive']`` on the ``as_of``
  read — the include_origin contract;
* ``live_gate_sql('f')`` is the exact expected string — the ONE rendering.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.provenance import AnalystContext, FactPayload, write_fact
from legba.data.provenance import origin as _origin
from legba.data.provenance.origin import (
    LIVE_CLASSES,
    ORIGIN_CLASSES,
    ORIGIN_CLASS_VERSION,
    live_gate_sql,
    origin_class_for,
)
from legba.data.research_evidence import land_research_row
from legba.data.seed import run_seed_source
from legba.data.seed.adapters.world_baseline import WorldBaselineSeedSource
from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort

_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def port(pg_pool):
    return PostgresQdrantSubstrateQueryPort(
        pg_pool=pg_pool, qdrant_client=None,
        signals_collection="legba_test_origin__signals")


def _analyst_ctx() -> AnalystContext:
    return AnalystContext(
        analyst_id=f"analyst.test_{uuid4().hex[:8]}",
        analyst_version="v" + uuid4().hex[:8],
        run_id=uuid4(),
        target_id="p7_origin_test",
        target_version="abc123def456",
    )


# ---------------------------------------------------------------------------
# The vocabulary + renderers (unit, no DB)
# ---------------------------------------------------------------------------


def test_live_gate_sql_is_the_exact_rendering() -> None:
    """The live gate is ONE string — the 0032 pair plus the class leg."""
    assert live_gate_sql("f") == (
        "f.superseded_by IS NULL AND f.valid_until IS NULL"
        " AND f.origin_class IN ('live','web_retrieval','seed')"
    )
    assert live_gate_sql("") == (
        "superseded_by IS NULL AND valid_until IS NULL"
        " AND origin_class IN ('live','web_retrieval','seed')"
    )


def test_origin_class_for_maps_web_origins_only() -> None:
    """web_search:*/web_evidence → web_retrieval; everything else → live.
    The history classes are NEVER derived — nothing live writes them."""
    assert origin_class_for("web_search:brave") == "web_retrieval"
    assert origin_class_for("web_evidence") == "web_retrieval"
    assert origin_class_for("web_evidence:tavily") == "web_retrieval"
    assert origin_class_for("curated_source") == "live"
    assert origin_class_for(None) == "live"
    assert origin_class_for("anything-unrecognised") == "live"


def test_origin_class_clause_validates_and_orders() -> None:
    """include_origin renders in canonical order; unknown → loud refusal."""
    assert _origin.origin_class_clause(
        "", ["seed", "live", "web_retrieval"]
    ) == "origin_class IN ('live','web_retrieval','seed')"
    with pytest.raises(_origin.OriginClassError):
        _origin.origin_class_clause("", ["archive", "nonsense"])
    with pytest.raises(_origin.OriginClassError):
        _origin.origin_class_clause("", [])


def test_vocabulary_and_version_markers() -> None:
    assert ORIGIN_CLASS_VERSION == "2026-09/p7"
    assert set(ORIGIN_CLASSES) == {
        "live", "web_retrieval", "seed",
        "backfill_native", "backfill_reconstructed", "archive",
    }
    assert LIVE_CLASSES == frozenset({"live", "web_retrieval", "seed"})
    assert LIVE_CLASSES <= set(ORIGIN_CLASSES)


# ---------------------------------------------------------------------------
# Write-path stamps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_fact_insert_reads_back_live(pg_conn) -> None:
    """A fact written through ``write_fact`` with no batch marker is 'live'."""
    subj = f"P7Live_{uuid4().hex[:8]}"
    await write_fact(
        pg_conn,
        analyst_ctx=_analyst_ctx(),
        payload=FactPayload(
            subject=subj,
            predicate="located in",
            value="Berlin",
        ),
        derived_from=[],
    )
    cls = await pg_conn.fetchval(
        "SELECT origin_class FROM facts WHERE subject = $1", subj)
    assert cls == "live"


@pytest.mark.asyncio
async def test_seed_batch_through_the_driver_reads_back_seed(pg_pool) -> None:
    """Every fact the seed driver writes is 'seed' — batch-scoped read."""
    result = await run_seed_source(
        pg_pool, WorldBaselineSeedSource(), dry_run=False)
    assert result.seed_batch_id is not None
    async with pg_pool.acquire() as conn:
        bad = await conn.fetchval(
            "SELECT count(*) FROM facts WHERE seed_batch_id IS NOT NULL"
            " AND origin_class <> 'seed'")
    # On a full re-seed (a sibling ran the adapter first) this batch stamps
    # no new rows — the invariant still holds over ALL batch-marked rows,
    # fresh or 0209-backfilled.
    assert bad == 0


@pytest.mark.asyncio
async def test_research_signal_reads_back_web_retrieval(pg_conn) -> None:
    """land_research_row stamps origin_class_for(retrieval_origin)."""
    sid = await land_research_row(pg_conn, {
        "id": uuid4(),
        "source_id": "research:test",
        "source_version": "v1",
        "produced_by_id": "test.run",
        "produced_by_kind": "research",
        "fetched_at": datetime.now(timezone.utc),
        "owner_tenant": "default",
        "modality": "text",
        "retention_class": "reference_only",
        "object_ref": None,
        "payload": {"title": "t"},
        "canonical_url": "https://example.test/p7",
        "raw_provenance": {},
        "language": "en",
        "geo": [],
        "tags": [],
        "entity_classes": [],
        "source_credibility": 0.5,
        "content_hash": f"h-{uuid4().hex}",
        "derived_from": [],
        "schema_uri": "iglu:legba/signal/jsonschema/3-0-0",
        "retrieval_origin": "web_search:test_provider",
        "salience": {},
    })
    assert sid is not None
    cls = await pg_conn.fetchval(
        "SELECT origin_class FROM signals WHERE id = $1", sid)
    assert cls == "web_retrieval"
    assert cls == origin_class_for("web_search:test_provider")


@pytest.mark.asyncio
async def test_history_class_write_fails_naming_the_seam(pg_conn) -> None:
    """The history_writer_not_built CHECK refuses a premature history-class row —
    and the error names the constraint, which is the seam."""
    from asyncpg.exceptions import CheckViolationError
    with pytest.raises(CheckViolationError) as excinfo:
        await pg_conn.execute(
            "INSERT INTO facts (id, subject, predicate, value, origin_class) "
            "VALUES ($1, 'x', 'y', 'z', 'backfill_native')",
            uuid4())
    assert "facts_origin_class_history_writer_not_built" in str(excinfo.value)


@pytest.mark.asyncio
async def test_supersede_stamps_superseded_at(pg_conn, monkeypatch) -> None:
    """A closed fact carries the decision-time stamp (0209)."""
    monkeypatch.setenv("LEGBA_FACT_CONTENTION", "0")
    subj = f"P7Sup_{uuid4().hex[:8]}"
    await write_fact(
        pg_conn, analyst_ctx=_analyst_ctx(),
        payload=FactPayload(subject=subj, predicate="leader", value="Old"),
        derived_from=[])
    await write_fact(
        pg_conn, analyst_ctx=_analyst_ctx(),
        payload=FactPayload(subject=subj, predicate="leader", value="New"),
        derived_from=[])
    row = await pg_conn.fetchrow(
        "SELECT superseded_by, superseded_at FROM facts "
        "WHERE subject = $1 AND valid_until IS NOT NULL", subj)
    assert row is not None
    assert row["superseded_by"] is not None
    assert row["superseded_at"] is not None


# ---------------------------------------------------------------------------
# The firewall — the archive row is invisible to the live read, visible when
# the reader asks for its class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_row_behind_the_firewall(pg_pool, port) -> None:
    """Drop the seam constraint, force 'archive', assert the firewall —
    then RESTORE the constraint and delete the row so the seam stays armed
    for every later test (the port reads on pool connections, so the forced
    row must be committed to be seen at all)."""
    subj = f"P7Arch_{uuid4().hex[:8]}"
    rid = uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE facts "
            "DROP CONSTRAINT facts_origin_class_history_writer_not_built")
        await conn.execute(
            "INSERT INTO facts (id, subject, predicate, value, "
            "                  valid_from, origin_class) "
            "VALUES ($1, $2, 'leader', 'Archivist', "
            "       '2016-01-01'::timestamptz, 'archive')",
            rid, subj)
        try:
            # The port's OPEN read — live classes only — never sees it.
            open_out = await port.query_facts(subject=subj)
            assert open_out["rows"] == []

            # The as-of read defaults to LIVE_CLASSES — still invisible.
            asof_default = await port.query_facts(
                subject=subj, as_of="2020-06-01")
            assert asof_default["rows"] == []

            # include_origin=['archive'] is the explicit ask that sees it.
            asof_arch = await port.query_facts(
                subject=subj, as_of="2020-06-01",
                include_origin=["archive"])
            assert len(asof_arch["rows"]) == 1
            assert asof_arch["rows"][0]["subject"] == subj
        finally:
            # Rearm the seam exactly as the migration declared it, and drop
            # the forced row — committed DDL/rows must not leak.
            await conn.execute("DELETE FROM facts WHERE id = $1", rid)
            await conn.execute(
                "ALTER TABLE facts "
                "ADD CONSTRAINT facts_origin_class_history_writer_not_built "
                "CHECK (origin_class IN ('live','web_retrieval','seed'))")


@pytest.mark.asyncio
async def test_include_origin_unknown_class_refuses_loud(pg_pool, port) -> None:
    out = await port.query_facts(
        subject="x", as_of="2020-06-01", include_origin=["nonsense"])
    assert out["rows"] == [] and "error" in out


# ---------------------------------------------------------------------------
# The poisoned-import guard — origin.py is a leaf in the slim image
# ---------------------------------------------------------------------------


def test_origin_module_imports_without_the_runtime_stack() -> None:
    """The slim-image probe: heavy third-party modules poisoned to ``None``,
    ``provenance.origin`` must still import and render its gate strings."""
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None

        import legba.data.provenance.origin as o

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts")
               or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from origin.py: %r" % leaked
        assert o.ORIGIN_CLASS_VERSION == "2026-09/p7"
        assert o.live_gate_sql("f").startswith("f.superseded_by IS NULL")
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, (
        "provenance.origin no longer imports without the runtime stack:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "OK" in result.stdout
