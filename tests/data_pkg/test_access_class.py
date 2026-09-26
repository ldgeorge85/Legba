# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wave-E — the access-class mechanism (migration 0216,
``data/provenance/access.py``). Mirrors ``test_origin_class.py``'s shape for
the sibling axis, one narrower: this pass builds the vocabulary, the
``signals.access_class`` stamp + backfill, and the opt-in read-side filter —
SEAMS #59 is explicit that NOTHING enforces it yet, so there is no
firewall/gate test here (contrast ``test_history_class_write_fails_naming_
the_seam`` on the origin side, which exists precisely because origin_class
DOES gate).

Covers:
* the vocabulary + version marker;
* ``access_class_clause`` validates + orders, raises on unknown/empty
  (mirrors ``origin_class_clause``, but with NO default set — this filter is
  opt-in only, never implicitly applied);
* ``access_ceiling_sql`` is the exact expected rendering;
* migration 0216 landed by name, with the ``signals_access_class_vocab``
  CHECK constraint in place;
* the backfill statement (migration 0216's own UPDATE, scoped by
  ``source_id`` for test isolation against the shared pivot DB) actually
  moves a fixture row from the fail-closed column default to the
  descriptor-head's classified value;
* the leaf import guard — ``access.py`` loads without the runtime stack.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.provenance.access import (
    ACCESS_CLASSES,
    ACCESS_CLASS_VERSION,
    AccessClassError,
    access_ceiling_sql,
    access_class_clause,
)

_SRC = str(pathlib.Path(__file__).resolve().parents[2] / "src")


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()


# ---------------------------------------------------------------------------
# The vocabulary + renderers (unit, no DB)
# ---------------------------------------------------------------------------


def test_vocabulary_and_version_markers() -> None:
    assert ACCESS_CLASS_VERSION == "2026-09/waveE"
    assert set(ACCESS_CLASSES) == {
        "public", "licensed_commercial", "licensed_noncommercial",
        "restricted", "internal",
    }
    # Restriction order (index = rank) is load-bearing for the ceiling.
    assert ACCESS_CLASSES == (
        "public", "licensed_commercial", "licensed_noncommercial",
        "restricted", "internal",
    )


def test_access_class_clause_validates_and_orders() -> None:
    assert access_class_clause(
        "s", ["restricted", "public", "licensed_commercial"]
    ) == "s.access_class IN ('public','licensed_commercial','restricted')"
    assert access_class_clause("", ["internal"]) == "access_class IN ('internal')"
    with pytest.raises(AccessClassError):
        access_class_clause("s", ["nonsense"])
    with pytest.raises(AccessClassError):
        access_class_clause("s", [])


def test_access_ceiling_sql_is_the_exact_rendering() -> None:
    """SEAMS #59 — the unwired ceiling helper renders one exact string."""
    rendered = access_ceiling_sql("f")
    assert rendered == (
        "COALESCE((SELECT s.access_class FROM public.signals s "
        "WHERE s.id = ANY(f.derived_from) "
        "ORDER BY array_position(ARRAY['public','licensed_commercial',"
        "'licensed_noncommercial','restricted','internal']::text[], "
        "s.access_class) DESC LIMIT 1), 'public')"
    )
    # Empty alias renders with no dotted prefix, mirroring live_gate_sql("").
    assert access_ceiling_sql("").startswith(
        "COALESCE((SELECT s.access_class FROM public.signals s "
        "WHERE s.id = ANY(derived_from)"
    )


# ---------------------------------------------------------------------------
# Migration 0216 landed, by name, with its constraint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_0216_landed_by_name(pg_conn) -> None:
    row = await pg_conn.fetchval(
        "SELECT count(*) FROM legba_data_migrations WHERE name LIKE $1",
        "0216_access_class%",
    )
    assert row and row >= 1


@pytest.mark.asyncio
async def test_signals_access_class_column_and_vocab_constraint(pg_conn) -> None:
    col = await pg_conn.fetchrow(
        "SELECT column_default, is_nullable FROM information_schema.columns "
        "WHERE table_name = 'signals' AND column_name = 'access_class'"
    )
    assert col is not None
    assert col["is_nullable"] == "NO"
    assert "restricted" in col["column_default"]

    con = await pg_conn.fetchval(
        "SELECT count(*) FROM pg_constraint WHERE conname = "
        "'signals_access_class_vocab'"
    )
    assert con == 1


@pytest.mark.asyncio
async def test_history_class_write_refuses_off_vocab(pg_conn) -> None:
    """The vocab CHECK refuses a value outside the closed set — the same
    discipline as `signals_origin_class_vocab`, without a second
    'readers_not_swept' leg (nothing gates access_class yet — SEAMS #59)."""
    from asyncpg.exceptions import CheckViolationError

    with pytest.raises(CheckViolationError) as excinfo:
        await pg_conn.execute(
            "INSERT INTO signals (id, source_id, fetched_at, access_class) "
            "VALUES ($1, 'source.test.waveE', now(), 'nonsense')",
            uuid4(),
        )
    assert "signals_access_class_vocab" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The backfill — migration 0216's own UPDATE, scoped to one fixture source_id
# for isolation against the shared pivot DB (the unscoped statement already
# ran once, at migrated_pg bootstrap, over a table with no classified heads
# yet — this proves the SAME statement moves a row once a head IS classified).
# ---------------------------------------------------------------------------

_BACKFILL_SQL = """
UPDATE public.signals s
   SET access_class = COALESCE(sd.body->'scope'->>'access_class', 'restricted')
  FROM public.source_descriptors sd
 WHERE sd.descriptor_id = s.source_id
   AND sd.is_head
   AND sd.body->'scope'->>'access_class' IS NOT NULL
   AND s.source_id = $1
"""


@pytest.mark.asyncio
async def test_backfill_moves_a_row_from_default_to_the_classified_head(
    pg_conn,
) -> None:
    source_id = f"source.test.waveE_backfill_{uuid4().hex[:8]}"
    signal_id = uuid4()
    try:
        # A classified head descriptor — same shape descriptor.py's
        # _insert_row writes for Family.SOURCE.
        await pg_conn.execute(
            """
            INSERT INTO source_descriptors
                (descriptor_id, version, schema_uri, is_head,
                 abstraction_level, kind, state, owner, name, body,
                 inherits, created_at)
            VALUES ($1, $2, 'legba/source/1.0.0', true,
                    'L1', 'rss', 'active', 't:waveE', 'backfill fixture',
                    $3::jsonb, '{}', now())
            """,
            source_id, "f" * 16,
            '{"scope": {"access_class": "licensed_commercial"}}',
        )
        # A pre-migration-shaped row: the column default applies (no
        # access_class passed), exactly what an un-backfilled row looked
        # like before 0216's own UPDATE ran.
        await pg_conn.execute(
            "INSERT INTO signals (id, source_id, fetched_at) VALUES "
            "($1, $2, now())",
            signal_id, source_id,
        )
        before = await pg_conn.fetchval(
            "SELECT access_class FROM signals WHERE id = $1", signal_id
        )
        assert before == "restricted"  # the fail-closed column default

        await pg_conn.execute(_BACKFILL_SQL, source_id)

        after = await pg_conn.fetchval(
            "SELECT access_class FROM signals WHERE id = $1", signal_id
        )
        assert after == "licensed_commercial"
    finally:
        await pg_conn.execute("DELETE FROM signals WHERE id = $1", signal_id)
        await pg_conn.execute(
            "DELETE FROM source_descriptors WHERE descriptor_id = $1", source_id
        )


@pytest.mark.asyncio
async def test_backfill_leaves_an_unclassified_head_at_the_default(
    pg_conn,
) -> None:
    """A head descriptor with NO access_class in its body (the pre-Wave-E
    shape, and the live tower's current shape — see migration 0216's
    header) leaves the row at 'restricted', never widening it."""
    source_id = f"source.test.waveE_nobackfill_{uuid4().hex[:8]}"
    signal_id = uuid4()
    try:
        await pg_conn.execute(
            """
            INSERT INTO source_descriptors
                (descriptor_id, version, schema_uri, is_head,
                 abstraction_level, kind, state, owner, name, body,
                 inherits, created_at)
            VALUES ($1, $2, 'legba/source/1.0.0', true,
                    'L1', 'rss', 'active', 't:waveE', 'no-access fixture',
                    $3::jsonb, '{}', now())
            """,
            source_id, "e" * 16, '{"scope": {}}',
        )
        await pg_conn.execute(
            "INSERT INTO signals (id, source_id, fetched_at) VALUES "
            "($1, $2, now())",
            signal_id, source_id,
        )
        await pg_conn.execute(_BACKFILL_SQL, source_id)
        after = await pg_conn.fetchval(
            "SELECT access_class FROM signals WHERE id = $1", signal_id
        )
        assert after == "restricted"
    finally:
        await pg_conn.execute("DELETE FROM signals WHERE id = $1", signal_id)
        await pg_conn.execute(
            "DELETE FROM source_descriptors WHERE descriptor_id = $1", source_id
        )


# ---------------------------------------------------------------------------
# The poisoned-import guard — access.py is a leaf in the slim image
# ---------------------------------------------------------------------------


def test_access_module_imports_without_the_runtime_stack() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None

        import legba.data.provenance.access as a

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts")
               or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from access.py: %r" % leaked
        assert a.ACCESS_CLASS_VERSION == "2026-09/waveE"
        assert a.access_ceiling_sql("f").startswith("COALESCE((SELECT")
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
        "provenance.access no longer imports without the runtime stack:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert "OK" in result.stdout
