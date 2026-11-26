# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scripts/unalias_valueless_id_urls_once.py`` against a real
migrated Postgres: a dry run writes nothing; ``--apply`` restores every false
tier-1 alias of a SPIP ``?article<N>`` host to its own canonical, re-points a
genuine re-ingest at its article's first row, leaves a non-tier-1 link alone,
backs every touched row up first, and a re-run changes nothing.

The rows live on a host unique to the test, so foreign rows (the N-S
equivalence corpus also writes ``lefaso.net`` URLs) can never enter the plan.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

from legba.data.config import PostgresConfig

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "unalias_valueless_id_urls_once.py"
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
SOURCE = "source.nu.spip"
# The script's own backup table (scripts/unalias_valueless_id_urls_once.py
# BACKUP_TABLE): created by --apply, dropped again when the test ends.
BACKUP_TABLE = "signal_alias_backup_n_u"


def _load():
    spec = importlib.util.spec_from_file_location("unalias_n_u", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["unalias_n_u"] = mod      # @dataclass resolves its module here
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    try:
        yield c
    finally:
        # Leave the session database as found (nightly shuffled seed 734222642:
        # the script's backup table, which no migration declares, failed
        # test_schema_hygiene's "exactly what the migrations declare" when this
        # ran first). The rows live on this test's own source id.
        try:
            await c.execute(f"DROP TABLE IF EXISTS {BACKUP_TABLE}")
            await c.execute("DELETE FROM signal_aliases WHERE alias_signal_id IN "
                            "(SELECT id FROM signals WHERE source_id = $1)", SOURCE)
            await c.execute("DELETE FROM signals WHERE source_id = $1", SOURCE)
        finally:
            await c.close()


async def _row(conn, url: str, minute: int, canonical: UUID | None = None, *,
               reason: str = "ingest_url") -> UUID:
    sid = uuid4()
    await conn.execute(
        "INSERT INTO signals (id, source_id, owner_tenant, modality, payload, canonical_url, "
        " content_hash, fetched_at, canonical_signal_id) VALUES ($1, 'source.nu.spip', "
        " 'shared', 'text', '{}'::jsonb, $2, $3, $4, $5)",
        sid, url, uuid4().hex, T0 + timedelta(minutes=minute), canonical or sid)
    if canonical is not None:
        await conn.execute(
            "INSERT INTO signal_aliases (alias_signal_id, canonical_signal_id, reason, score, "
            " produced_by) VALUES ($1, $2, $3, 1.0, 'ingest_dedupe:source.nu.spip')",
            sid, canonical, reason)
    return sid


async def _state(conn, ids):
    rows = await conn.fetch(
        "SELECT s.id, s.canonical_signal_id, s.indexed_at, "
        " array(SELECT a.canonical_signal_id FROM signal_aliases a "
        "        WHERE a.alias_signal_id = s.id ORDER BY 1) AS links "
        "FROM signals s WHERE s.id = ANY($1::uuid[])", list(ids))
    return {r["id"]: (r["canonical_signal_id"], list(r["links"])) for r in rows}


async def test_dry_run_then_apply_restores_false_aliases(conn):
    mod = _load()
    assert mod.BACKUP_TABLE == BACKUP_TABLE  # the table the fixture drops
    host = f"spip-{uuid4().hex[:10]}.example"
    base = f"https://{host}/spip.php"
    root = await _row(conn, f"{base}?article100", 0)                      # the old collapse root
    a101 = await _row(conn, f"{base}?article101", 1, root)                 # false alias
    a102 = await _row(conn, f"https://www.{host}/spip.php?article102", 2, root)  # false alias
    a101b = await _row(conn, f"{base}?article101&utm_source=rss", 3, root)     # same article,
    #   but utm_source is a valued param: a different canonical form -> its own canonical
    a102b = await _row(conn, f"{base}?article102#forum", 4, root)          # true re-ingest of 102
    other = await _row(conn, f"{base}?article103", 5, root, reason="near_duplicate")  # not tier 1
    ids = [root, a101, a102, a101b, a102b, other]

    before = await _state(conn, ids)
    p = await mod.run(conn, hosts=(host,), apply=False, quiet=True)
    assert await _state(conn, ids) == before                               # dry run: no writes
    assert (p.assessed, p.restored_self, p.repointed, p.skipped_other_link) == (6, 3, 1, 1)

    await mod.run(conn, hosts=(host,), apply=True, quiet=True)
    after = await _state(conn, ids)
    assert after[root] == (root, [])
    assert after[a101] == (a101, [])
    assert after[a102] == (a102, [])
    assert after[a101b] == (a101b, [])
    assert after[a102b] == (a102, [a102])                                  # re-pointed
    assert after[other] == (root, [root])                                  # untouched
    # every touched row was backed up with its PRE-repair state, and marked dirty
    backup = {r["id"]: r for r in await conn.fetch(
        f"SELECT * FROM {mod.BACKUP_TABLE} WHERE id = ANY($1::uuid[])", ids)}
    assert set(backup) == {a101, a102, a101b, a102b}
    assert all(b["canonical_signal_id"] == root for b in backup.values())
    assert json.loads(backup[a101]["alias_links"])[0]["reason"] == "ingest_url"
    dirty = await conn.fetch(
        "SELECT id FROM signals WHERE id = ANY($1::uuid[]) AND indexed_at IS NULL", ids)
    assert {r["id"] for r in dirty} >= {a101, a102, a101b, a102b}

    again = await mod.run(conn, hosts=(host,), apply=True, quiet=True)
    assert again.changes == [] and await _state(conn, ids) == after       # idempotent
