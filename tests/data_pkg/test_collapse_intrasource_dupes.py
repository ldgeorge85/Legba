# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Historical intra-source dupe collapse script (W-2a).

Exercises ``scripts/collapse_intrasource_dupes.py`` against the ephemeral
migrated test DB (the ``migrated_pg`` session fixture):

  * survivor election — the NEWEST-fetched row survives (live S-4 pick);
  * every reference class is re-pointed BEFORE the loser delete
    (entity links / aliases / canonical pointer / uuid[] lineage arrays /
    evidence_archive sidecar + object_ref mirror);
  * distinct content is never touched (different hash, different source,
    different tenant, empty hash);
  * retention keep-set losers are never deleted;
  * idempotency — a second run finds nothing and changes nothing;
  * dry-run writes nothing and reports exact counts.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig

# Import the script as a module (scripts/ is not a package). Resolve relative
# to this test file so the MAIN checkout and worktrees each test their own copy.
_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "collapse_intrasource_dupes.py"
_spec = importlib.util.spec_from_file_location("collapse_intrasource_dupes", _SCRIPT)
collapse = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(collapse)


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    yield c
    await c.close()


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


async def _insert_signal(
    conn, *, sid=None, source="src.dupes", tenant="t_dupes", chash="H",
    fetched_at=None, retention="reference_only", canonical=None,
    derived_from=(), object_ref=None,
) -> object:
    sid = sid or uuid4()
    await conn.execute(
        """
        INSERT INTO signals (id, source_id, owner_tenant, modality, payload,
                             content_hash, fetched_at, retention_class,
                             canonical_signal_id, derived_from, object_ref)
        VALUES ($1,$2,$3,'text',$4::jsonb,$5,$6,$7,$8,$9::uuid[],$10)
        """,
        sid, source, tenant, json.dumps({"title": "x"}), chash,
        fetched_at or _now(), retention, canonical, list(derived_from),
        object_ref,
    )
    return sid


async def _run(conn, **kw):
    kw.setdefault("quiet", True)
    return await collapse.run(conn, **kw)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_survivor_election_newest_and_full_repoint(conn):
    """3-row group: newest survives; every reference class re-points."""
    tenant = f"t_{uuid4().hex[:8]}"
    src = f"src.{uuid4().hex[:6]}"
    h = f"h_{uuid4().hex}"
    t0 = _now() - timedelta(days=3)

    old = await _insert_signal(conn, source=src, tenant=tenant, chash=h, fetched_at=t0)
    mid = await _insert_signal(conn, source=src, tenant=tenant, chash=h,
                               fetched_at=t0 + timedelta(hours=1))
    new = await _insert_signal(conn, source=src, tenant=tenant, chash=h,
                               fetched_at=t0 + timedelta(hours=2))

    # Reference rows against the LOSERS (old + mid).
    ent = uuid4()
    other = await _insert_signal(conn, source=src, tenant=tenant,
                                 chash=f"h_{uuid4().hex}")
    pointing = await _insert_signal(conn, source=src, tenant=tenant,
                                    chash=f"h_{uuid4().hex}", canonical=old)
    derived = await _insert_signal(conn, source=src, tenant=tenant,
                                   chash=f"h_{uuid4().hex}",
                                   derived_from=[mid, other])
    await conn.execute(
        "INSERT INTO entity_profiles (id, canonical_name, entity_type, "
        "entity_class, data, completeness_score) "
        "VALUES ($1,$2,'location','location','{}'::jsonb,0.3)",
        ent, f"Place_{tenant}",
    )
    await conn.execute(
        "INSERT INTO signal_entity_links (signal_id, entity_id, role, confidence) "
        "VALUES ($1,$2,'mentioned',0.8)", old, ent)
    # The survivor ALREADY has the same link — re-point must dedup, not error.
    await conn.execute(
        "INSERT INTO signal_entity_links (signal_id, entity_id, role, confidence) "
        "VALUES ($1,$2,'mentioned',0.9)", new, ent)
    await conn.execute(
        "INSERT INTO signal_aliases (alias_signal_id, canonical_signal_id, reason) "
        "VALUES ($1,$2,'x')", old, other)
    await conn.execute(
        "INSERT INTO signal_aliases (alias_signal_id, canonical_signal_id, reason) "
        "VALUES ($1,$2,'y')", other, mid)
    fact = uuid4()
    await conn.execute(
        "INSERT INTO facts (id, subject, predicate, value, derived_from) "
        "VALUES ($1,'s','p','v',$2::uuid[])", fact, [old, other, new])
    out_row = uuid4()
    await conn.execute(
        "INSERT INTO analyst_outputs (id, kind, title, schema_uri, derived_from) "
        "VALUES ($1,'finding','t','iglu:x',$2::uuid[])", out_row, [mid])
    # Archive sidecar on a loser; survivor has none and no object_ref.
    await conn.execute(
        "INSERT INTO evidence_archive (signal_id, status, object_ref, sha256) "
        "VALUES ($1,'archived','cas:sha256/abc','abc')", old)

    res = await _run(conn, apply=True, tenant=tenant)
    assert res["signals_deleted"] == 2

    # Survivor kept, losers gone.
    assert await conn.fetchval("SELECT 1 FROM signals WHERE id=$1", new) == 1
    for loser in (old, mid):
        assert await conn.fetchval("SELECT 1 FROM signals WHERE id=$1", loser) is None

    # Entity link re-pointed + deduped onto the survivor.
    links = await conn.fetch(
        "SELECT signal_id FROM signal_entity_links WHERE entity_id=$1", ent)
    assert [r["signal_id"] for r in links] == [new]

    # Aliases re-pointed on both sides, none referencing losers.
    assert await conn.fetchval(
        "SELECT count(*) FROM signal_aliases WHERE alias_signal_id = ANY($1::uuid[]) "
        "OR canonical_signal_id = ANY($1::uuid[])", [old, mid]) == 0
    assert await conn.fetchval(
        "SELECT 1 FROM signal_aliases WHERE alias_signal_id=$1 "
        "AND canonical_signal_id=$2", new, other) == 1
    assert await conn.fetchval(
        "SELECT 1 FROM signal_aliases WHERE alias_signal_id=$1 "
        "AND canonical_signal_id=$2", other, new) == 1

    # canonical_signal_id re-pointed.
    assert await conn.fetchval(
        "SELECT canonical_signal_id FROM signals WHERE id=$1", pointing) == new

    # uuid[] arrays re-pointed, order-preserving, survivor de-duplicated.
    assert await conn.fetchval(
        "SELECT derived_from FROM signals WHERE id=$1", derived) == [new, other]
    assert await conn.fetchval(
        "SELECT derived_from FROM facts WHERE id=$1", fact) == [new, other]
    assert await conn.fetchval(
        "SELECT derived_from FROM analyst_outputs WHERE id=$1", out_row) == [new]

    # Archive sidecar re-pointed to the survivor + object_ref mirrored.
    assert await conn.fetchval(
        "SELECT signal_id FROM evidence_archive WHERE signal_id=$1", new) == new
    assert await conn.fetchval(
        "SELECT object_ref FROM signals WHERE id=$1", new) == "cas:sha256/abc"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_distinct_content_never_touched(conn):
    """Different hash / different source / different tenant / empty hash all
    survive untouched — only exact (source, hash, tenant) groups collapse."""
    tenant = f"t_{uuid4().hex[:8]}"
    src_a = f"src.{uuid4().hex[:6]}"
    src_b = f"src.{uuid4().hex[:6]}"
    h = f"h_{uuid4().hex}"

    keep = [
        await _insert_signal(conn, source=src_a, tenant=tenant, chash=h),
        await _insert_signal(conn, source=src_a, tenant=tenant,
                             chash=f"h_{uuid4().hex}"),          # different hash
        await _insert_signal(conn, source=src_b, tenant=tenant, chash=h),  # other source
        await _insert_signal(conn, source=src_a, tenant=f"t2_{uuid4().hex[:6]}",
                             chash=h),                            # other tenant
        await _insert_signal(conn, source=src_a, tenant=tenant, chash=""),
        await _insert_signal(conn, source=src_a, tenant=tenant, chash=""),  # empty hash x2
    ]

    before = await conn.fetchval("SELECT count(*) FROM signals WHERE id = ANY($1::uuid[])", keep)
    res = await _run(conn, apply=True, source=src_a)
    after = await conn.fetchval("SELECT count(*) FROM signals WHERE id = ANY($1::uuid[])", keep)
    assert before == after == len(keep)
    # src_a scope had no duplicate group at all.
    assert not any(s["source_id"] == src_a for s in res["samples"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_idempotent_second_run_is_a_noop(conn):
    tenant = f"t_{uuid4().hex[:8]}"
    src = f"src.{uuid4().hex[:6]}"
    h = f"h_{uuid4().hex}"
    t0 = _now() - timedelta(days=1)
    await _insert_signal(conn, source=src, tenant=tenant, chash=h, fetched_at=t0)
    await _insert_signal(conn, source=src, tenant=tenant, chash=h,
                         fetched_at=t0 + timedelta(minutes=5))

    first = await _run(conn, apply=True, tenant=tenant)
    assert first["signals_deleted"] == 1
    second = await _run(conn, apply=True, tenant=tenant)
    assert second["signals_deleted"] == 0
    assert second["groups_seen"] == 0
    assert await conn.fetchval(
        "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_keepset_losers_never_deleted(conn):
    """An evidence_hold duplicate is skipped (held), the plain one collapses."""
    tenant = f"t_{uuid4().hex[:8]}"
    src = f"src.{uuid4().hex[:6]}"
    h = f"h_{uuid4().hex}"
    t0 = _now() - timedelta(days=1)
    held = await _insert_signal(conn, source=src, tenant=tenant, chash=h,
                                fetched_at=t0, retention="evidence_hold")
    plain = await _insert_signal(conn, source=src, tenant=tenant, chash=h,
                                 fetched_at=t0 + timedelta(minutes=1))
    newest = await _insert_signal(conn, source=src, tenant=tenant, chash=h,
                                  fetched_at=t0 + timedelta(minutes=2))

    res = await _run(conn, apply=True, tenant=tenant)
    assert res["held_skipped"] == 1
    assert res["signals_deleted"] == 1
    assert await conn.fetchval("SELECT 1 FROM signals WHERE id=$1", held) == 1
    assert await conn.fetchval("SELECT 1 FROM signals WHERE id=$1", newest) == 1
    assert await conn.fetchval("SELECT 1 FROM signals WHERE id=$1", plain) is None

    # Re-run: the held group is re-seen but nothing more is deleted.
    again = await _run(conn, apply=True, tenant=tenant)
    assert again["signals_deleted"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dry_run_default_writes_nothing_and_counts_exactly(conn):
    tenant = f"t_{uuid4().hex[:8]}"
    src = f"src.{uuid4().hex[:6]}"
    h = f"h_{uuid4().hex}"
    t0 = _now() - timedelta(days=1)
    a = await _insert_signal(conn, source=src, tenant=tenant, chash=h, fetched_at=t0)
    b = await _insert_signal(conn, source=src, tenant=tenant, chash=h,
                             fetched_at=t0 + timedelta(minutes=5))
    ent = uuid4()
    await conn.execute(
        "INSERT INTO entity_profiles (id, canonical_name, entity_type, "
        "entity_class, data, completeness_score) "
        "VALUES ($1,$2,'location','location','{}'::jsonb,0.3)",
        ent, f"Place_{tenant}",
    )
    await conn.execute(
        "INSERT INTO signal_entity_links (signal_id, entity_id, role, confidence) "
        "VALUES ($1,$2,'mentioned',0.8)", a, ent)

    res = await _run(conn, tenant=tenant)  # apply defaults False
    assert res["would_delete"] == 1
    assert res["groups_seen"] == 1
    assert res["deleted_ids"] == [str(a)]  # a is older → the loser
    assert res["reference_rows"]["links_rows"] == 1
    assert res["samples"][0]["survivor"] == str(b)
    assert res["samples"][0]["losers"] == [str(a)]

    # Nothing changed.
    assert await conn.fetchval(
        "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant) == 2
    assert await conn.fetchval(
        "SELECT signal_id FROM signal_entity_links WHERE entity_id=$1", ent) == a


@pytest.mark.integration
@pytest.mark.asyncio
async def test_batching_collapses_across_transactions(conn):
    """batch_groups=1 forces one transaction per group; all groups collapse."""
    tenant = f"t_{uuid4().hex[:8]}"
    src = f"src.{uuid4().hex[:6]}"
    t0 = _now() - timedelta(days=1)
    for i in range(3):
        h = f"h_{uuid4().hex}"
        await _insert_signal(conn, source=src, tenant=tenant, chash=h, fetched_at=t0)
        await _insert_signal(conn, source=src, tenant=tenant, chash=h,
                             fetched_at=t0 + timedelta(minutes=i + 1))

    res = await _run(conn, apply=True, tenant=tenant, batch_groups=1)
    assert res["groups_seen"] == 3
    assert res["signals_deleted"] == 3
    assert await conn.fetchval(
        "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant) == 3
