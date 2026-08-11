# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0130 — quarantine the sub-floor embeddings.

The roadmap asked for a purge of the false semantic-dedup links. There are
none: `signal_aliases` holds only exact `ingest_url` / `content_hash` rows at
score 1.0 and ZERO `semantic_qdrant` rows, because the tier never issued a
Qdrant query in its history. The "50.5% wrong" figure was a simulation of what a
repaired pass WOULD link, not a count of rows.

The hazard is one level up: 36,733 of 59,994 vectored signals (61.2%, measured
2026-08-02) carry a vector built from an embed input under the length floor —
the degenerate class that scores cosine ~1.0 between unrelated stories and that
no threshold can separate. This migration moves them off their uuid marker onto
the `stale_subfloor` sentinel, which takes them out of the dedup tier from BOTH
directions without deleting a row or a point.

These tests run the SHIPPED SQL — the file is read, stripped of comments, and
executed against the pivot substrate — so they cannot pass against a migration
that says something different from the one that will run.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from legba.data.analysts.deterministic_handlers import signal_embedder
from legba.data.analysts.deterministic_handlers.cross_source_dedup import (
    _UUID_EMBEDDING_REF_RE,
)

MIGRATION = (
    Path(signal_embedder.__file__).parents[3]
    / "data" / "migrations" / "0130_quarantine_subfloor_embeddings.sql"
)

STALE_SUBFLOOR = "stale_subfloor"


def _statement() -> str:
    """The migration's SQL with the comment banner stripped."""
    text = MIGRATION.read_text(encoding="utf-8")
    body = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )
    return body.strip()


# ---------------------------------------------------------------------------
# Drift guards — the migration hardcodes what the embedder decides
# ---------------------------------------------------------------------------


def test_migration_file_exists_and_holds_exactly_one_statement():
    assert MIGRATION.is_file(), f"{MIGRATION} missing"
    sql = _statement()
    assert sql.count(";") == 1, "a data repair should be one auditable statement"
    assert sql.upper().startswith("UPDATE SIGNALS")
    # Soft repair only — the house rule is that evidence rows are never deleted.
    assert "DELETE" not in sql.upper()
    assert "DROP" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()


def test_migration_floor_matches_the_embedders_floor():
    """The predicate hardcodes 200 because SQL cannot import a constant. If the
    embedder's floor ever moves, this catches the migration going stale rather
    than letting it quietly quarantine the wrong cohort."""
    assert signal_embedder.MIN_BODY_CHARS == 200
    assert f") < {signal_embedder.MIN_BODY_CHARS};" in _statement()


def test_migration_body_precedence_matches_the_embedders():
    """The COALESCE chain must name the embedder's candidate fields, in the
    embedder's order — the cohort is defined by which field the OLD pick would
    have chosen."""
    sql = _statement()
    positions = [sql.index(f"payload->>'{field}'") for field in signal_embedder._BODY_FIELDS]
    assert positions == sorted(positions), (
        "the migration's COALESCE order has drifted from _BODY_FIELDS"
    )


def test_migration_targets_only_really_vectored_rows():
    """It must not touch rows already on a sentinel — those carry no vector to
    quarantine, and rewriting them would destroy the reason they were drained."""
    sql = _statement()
    assert _UUID_EMBEDDING_REF_RE in sql, (
        "the migration must select on the same uuid shape the dedup tier does"
    )


def test_quarantine_sentinel_is_rejected_by_the_dedup_eligibility_regex():
    """The whole mechanism: a quarantined row must fail the uuid test that gates
    both dedup candidates and dedup neighbours."""
    assert not re.match(_UUID_EMBEDDING_REF_RE, STALE_SUBFLOOR)
    assert re.match(_UUID_EMBEDDING_REF_RE, str(uuid4()))


# ---------------------------------------------------------------------------
# Behaviour — the shipped SQL against a real substrate
# ---------------------------------------------------------------------------

_PIVOT_DB = {
    "host": os.environ.get("LEGBA_PIVOT_PG_HOST", "127.0.0.1"),
    "port": int(os.environ.get("LEGBA_PIVOT_PG_PORT", "5432")),
    "user": os.environ.get("LEGBA_PIVOT_PG_USER", "legba"),
    "password": os.environ.get("LEGBA_PIVOT_PG_PASSWORD", "legba"),
    "database": os.environ.get("LEGBA_PIVOT_PG_DB", "legba_pivot_test"),
}


@pytest.fixture
async def pivot_pool():
    asyncpg = pytest.importorskip("asyncpg")
    try:
        pool = await asyncpg.create_pool(min_size=1, max_size=4, **_PIVOT_DB)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"legba_pivot_test unreachable: {exc}")
    async with pool.acquire() as conn:
        has_ref = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='signals' AND column_name='embedding_ref'"
        )
    if not has_ref:
        await pool.close()
        pytest.skip("pivot substrate has no signals.embedding_ref")
    yield pool
    await pool.close()


async def test_shipped_sql_quarantines_exactly_the_subfloor_cohort(pivot_pool):
    """Seed one row of every shape the predicate has to decide about, run the
    REAL migration statement, and check what moved."""
    tenant = f"mig0130_{uuid4().hex[:8]}"
    long_body = "x" * 400
    short_body = "(END)"

    # (label, payload, expected_to_be_quarantined)
    cases = [
        ("subfloor_first_field", {"raw_body": short_body}, True),
        ("subfloor_but_has_title", {"title": "A headline", "raw_body": short_body}, True),
        # THE precedence case: a stub in the FIRST field shadows a real body, so
        # the live vector is junk even though a good body exists on the row.
        ("stub_shadows_real_body",
         {"distilled_body": short_body, "raw_body": long_body}, True),
        ("real_body", {"raw_body": long_body}, False),
        # Precedence again, the other way: the first field is already good.
        ("real_first_field",
         {"distilled_body": long_body, "raw_body": short_body}, False),
        ("no_body_at_all", {"title": "bare"}, True),
    ]
    ids = {label: uuid4() for label, _payload, _expected in cases}

    try:
        async with pivot_pool.acquire() as conn:
            for label, payload, _expected in cases:
                sig = ids[label]
                await conn.execute(
                    """INSERT INTO signals
                           (id, source_id, owner_tenant, modality, payload,
                            content_hash, fetched_at, embedding_ref)
                       VALUES ($1,'src',$2,'text',$3::jsonb,'',$4,$5)""",
                    sig, tenant, json.dumps(payload),
                    datetime(2026, 8, 1, tzinfo=timezone.utc), str(sig),
                )
            # Rows already drained on a sentinel must be left alone.
            drained = uuid4()
            await conn.execute(
                """INSERT INTO signals
                       (id, source_id, owner_tenant, modality, payload,
                        content_hash, fetched_at, embedding_ref)
                   VALUES ($1,'src',$2,'text',$3::jsonb,'',$4,$5)""",
                drained, tenant, json.dumps({"raw_body": short_body}),
                datetime(2026, 8, 1, tzinfo=timezone.utc),
                signal_embedder._NO_BODY_MARKER,
            )

            await conn.execute(_statement())

            for label, _payload, expected in cases:
                ref = await conn.fetchval(
                    "SELECT embedding_ref FROM signals WHERE id=$1", ids[label])
                if expected:
                    assert ref == STALE_SUBFLOOR, f"{label} was not quarantined"
                else:
                    assert ref == str(ids[label]), (
                        f"{label} was quarantined but its vector is fine"
                    )
            assert await conn.fetchval(
                "SELECT embedding_ref FROM signals WHERE id=$1", drained,
            ) == signal_embedder._NO_BODY_MARKER, (
                "an already-drained row was overwritten — its drain reason is "
                "evidence and must survive"
            )
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)


async def test_quarantine_is_exactly_reversible(pivot_pool):
    """The prior value was ALWAYS the row's own id, so the documented rollback
    restores the exact state — nothing about the repair is lossy."""
    tenant = f"mig0130_rev_{uuid4().hex[:8]}"
    sig = uuid4()
    try:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO signals
                       (id, source_id, owner_tenant, modality, payload,
                        content_hash, fetched_at, embedding_ref)
                   VALUES ($1,'src',$2,'text',$3::jsonb,'',$4,$5)""",
                sig, tenant, json.dumps({"raw_body": "(END)"}),
                datetime(2026, 8, 1, tzinfo=timezone.utc), str(sig),
            )
            await conn.execute(_statement())
            assert await conn.fetchval(
                "SELECT embedding_ref FROM signals WHERE id=$1", sig,
            ) == STALE_SUBFLOOR

            # The rollback the migration documents.
            await conn.execute(
                "UPDATE signals SET embedding_ref = id::text "
                "WHERE embedding_ref = $1 AND owner_tenant = $2",
                STALE_SUBFLOOR, tenant,
            )
            assert await conn.fetchval(
                "SELECT embedding_ref FROM signals WHERE id=$1", sig,
            ) == str(sig)
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)


async def test_released_cohort_is_picked_up_by_the_embedder_scan(pivot_pool):
    """The documented release (`SET embedding_ref = NULL`) must actually put the
    row back in the embedder's un-embedded scan — otherwise the quarantine is a
    one-way trip and the re-embed can never happen."""
    tenant = f"mig0130_rel_{uuid4().hex[:8]}"
    sig = uuid4()
    try:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO signals
                       (id, source_id, owner_tenant, modality, payload,
                        content_hash, fetched_at, embedding_ref)
                   VALUES ($1,'src',$2,'text',$3::jsonb,'',$4,$5)""",
                sig, tenant, json.dumps({"raw_body": "(END)"}),
                datetime(2026, 8, 1, tzinfo=timezone.utc), str(sig),
            )
            await conn.execute(_statement())
            await conn.execute(
                "UPDATE signals SET embedding_ref = NULL "
                "WHERE embedding_ref = $1 AND owner_tenant = $2",
                STALE_SUBFLOOR, tenant,
            )
            scanned = await conn.fetch(
                signal_embedder._SELECT_BATCH_SQL.replace(
                    "WHERE embedding_ref IS NULL",
                    f"WHERE embedding_ref IS NULL AND owner_tenant = '{tenant}'",
                ),
                10,
            )
            assert [str(r["id"]) for r in scanned] == [str(sig)]
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)
