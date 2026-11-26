# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-6 — the entity name lookup through ``entity_alias_keys`` (migration 0263).

* the migration is idempotent (applied twice) and installs the table, its
  primary-key lookup index and the two sync triggers;
* the MATCHING CONTRACT (``_entity_resolve`` module docstring) holds on the
  scan, the key table and the shadow, case by case, on the REAL database;
* the key table cannot drift: an alias added, an alias removed, a merge, a
  split, a rename, a retirement and a delete each leave the verifier at zero;
  the ingestion upsert's ON CONFLICT branch does not fire the trigger;
* the backfill: dry run writes nothing, ``--apply`` fills, a re-run writes
  nothing, a cursor resumes;
* ``EXPLAIN``: both table probes are served by ``entity_alias_keys_pkey``;
* ``scan`` (and unset) sends exactly the scan statements (pinned hashes of the
  SQL text — the earlier d312da77 text, whose follow rule changed it
  deliberately on both paths — and the call sequence on a recording connection);
* a profile merged away (``merged_into`` set, whatever its
  ``gc_status``) is never elected; a match on it is followed to the end of
  its chain, on every path, single and batch;
  ``shadow`` serves the scan's row and counts the disagreement;
  ``resolve_keepers`` equals ``resolve_keeper`` per name in every mode.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg
import pytest
import pytest_asyncio

from legba.data import _entity_resolve as er
from legba.data._entity_resolve import resolve_keeper, resolve_keepers
from legba.data.config import PostgresConfig
from legba.data.entity_alias_keys import backfill_batch, verify_batch

_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src" / "legba" / "data" / "migrations" / "0263_entity_alias_keys.sql"
)
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

MODES = ("scan", "table", "shadow")


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def tag(pg_pool):
    """A per-test suffix (the session DB is shared, every name is unique); the
    test's profiles are deleted afterwards (one of them carries a non-array
    ``merged_aliases`` the ingestion scan would choke on)."""
    t = "p6" + uuid.uuid4().hex[:8]
    yield t
    async with pg_pool.acquire() as c:
        await c.execute("DELETE FROM entity_profiles WHERE canonical_name LIKE $1",
                        "%" + t + "%")


async def _seed(conn: Any, name: str, cls: str = "organization", *,
                aliases: Any = None, gc: str | None = None,
                created_at: str | None = None, eid: str | None = None) -> str:
    data: dict[str, Any] = {}
    if aliases is not None:
        data["merged_aliases"] = aliases
    if gc:
        data["gc_status"] = gc
    eid = eid or str(uuid.uuid4())
    ts = (datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc)
          if created_at else None)
    await conn.execute(
        """
        INSERT INTO entity_profiles (id, canonical_name, entity_class, entity_type,
                                     data, created_at)
        VALUES ($1::uuid, $2, $3, $3, $4::jsonb, COALESCE($5::timestamptz, now()))
        """,
        eid, name, cls, json.dumps(data), ts,
    )
    return eid


async def _drift(conn: Any, ids: list[str]) -> int:
    """Verifier drift over exactly these profiles (keyset batch of 1 each)."""
    total = 0
    for i in ids:
        prev = await conn.fetchval(
            "SELECT id::text FROM entity_profiles WHERE id < $1::uuid "
            "ORDER BY id DESC LIMIT 1", i)
        r = await verify_batch(conn, after_id=prev, batch_size=1)
        assert r.profiles == 1
        total += r.drift
    return total


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_migration_is_idempotent_and_complete(pg_pool) -> None:
    sql = _MIGRATION.read_text()
    assert "BEGIN;" not in sql.upper() and "COMMIT;" not in sql.upper()
    async with pg_pool.acquire() as conn:
        await conn.execute(sql)
        await conn.execute(sql)
        pk = await conn.fetchval(
            "SELECT pg_get_indexdef('public.entity_alias_keys_pkey'::regclass)")
        trig = await conn.fetch(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = 'public.entity_profiles'::regclass "
            "AND tgname LIKE 'entity_alias_keys_sync%' ORDER BY 1")
        fk = await conn.fetchval(
            "SELECT confdeltype::text || confupdtype::text FROM pg_constraint "
            "WHERE conrelid = 'public.entity_alias_keys'::regclass AND contype = 'f'")
    assert "(key_kind, key, entity_id)" in pk
    assert [r["tgname"] for r in trig] == [
        "entity_alias_keys_sync_insert", "entity_alias_keys_sync_update"]
    assert fk == "cc"  # ON DELETE CASCADE, ON UPDATE CASCADE


@pytest.mark.asyncio
async def test_key_function_matches_the_scan_expressions(pg_pool) -> None:
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key_kind, key FROM entity_alias_keys_for($1, $2::jsonb) ORDER BY 1, 2",
            "  The   Big  Thing ",
            json.dumps({"merged_aliases": ["  An Alias  Two", None, 7, "ÉCOLE"]}),
        )
        non_array = await conn.fetch(
            "SELECT key_kind, key FROM entity_alias_keys_for($1, $2::jsonb)",
            "X", json.dumps({"merged_aliases": "not-an-array"}))
    assert [(r["key_kind"], r["key"]) for r in rows] == [
        ("alias_lower", "7"),
        ("alias_lower", "an alias  two"),
        ("alias_lower", "école"),
        ("norm", "7"),
        ("norm", "alias two"),
        ("norm", "big thing"),
        ("norm", "école"),
    ]
    assert [(r["key_kind"], r["key"]) for r in non_array] == [("norm", "x")]


# ---------------------------------------------------------------------------
# The matching contract, on all three paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_contract_cases(pg_pool, tag, mode, monkeypatch) -> None:
    monkeypatch.setenv(er.ALIAS_LOOKUP_ENV, mode)
    t = tag
    async with pg_pool.acquire() as c:
        await _seed(c, f"Keeper Org {t}", aliases=[f"KO{t}", f"  Spaced {t}  "])
        await _seed(c, f"Strait {t}", "location")
        await _seed(c, f"Dead {t}", aliases=[f"deadalias{t}"], gc="merged")
        await _seed(c, f"Junk Keeper {t}", aliases=[f"junkalias{t}"], gc="junk")
        await _seed(c, f"Odd {t}", aliases=f"oddscalar{t}")
        await _seed(c, f"Nully {t}", aliases=[None, f"nullok{t}"])
        # E1.1: a bare 'entity' fragment row + an org claiming it as an alias
        await _seed(c, f"FRAG{t}", "entity")
        await _seed(c, f"Fragment Council {t}", "organization", aliases=[f"FRAG{t}"])
        # a classified exact row is never overridden
        await _seed(c, f"Palm{t}", "location")
        await _seed(c, f"Palm Org {t}", "organization", aliases=[f"Palm{t}"])
        # fallback class guard: the alias lives on a location
        await _seed(c, f"Ocean {t}", "location", aliases=[f"the Sea {t}"])
        # priority beats age; age beats everything below it
        await _seed(c, f"Old Person {t}", "person", aliases=[f"shared{t}"],
                    created_at="2020-01-01")
        await _seed(c, f"Young Country {t}", "country", aliases=[f"shared{t}"],
                    created_at="2026-01-01")
        await _seed(c, f"Older Org {t}", "organization", aliases=[f"agetie{t}"],
                    created_at="2021-01-01")
        await _seed(c, f"Newer Org {t}", "organization", aliases=[f"agetie{t}"],
                    created_at="2022-01-01")
        # no diacritic folding
        await _seed(c, f"École {t}", "organization")

        async def r(name: str, cls: str | None = None) -> str:
            return await resolve_keeper(c, name, entity_class=cls)

        assert await r(f"keeper org {t}") == f"Keeper Org {t}"            # exact, case
        assert await r(f"ko{t}") == f"Keeper Org {t}"                     # alias (norm)
        assert await r(f"spaced {t}") == f"Keeper Org {t}"                # alias btrim
        assert await r(f"The  strait {t}") == f"Strait {t}"               # article + ws
        assert await r(f"nobody{t}") == f"nobody{t}"                      # no match
        assert await r(f"deadalias{t}") == f"deadalias{t}"                # merged loser
        assert await r(f"junkalias{t}") == f"junkalias{t}"                # junk loser
        assert await r(f"oddscalar{t}") == f"oddscalar{t}"                # non-array
        assert await r(f"nullok{t}") == f"Nully {t}"                      # null element
        assert await r(f"FRAG{t}") == f"Fragment Council {t}"             # E1.1
        assert await r(f"FRAG{t}", "person") == f"FRAG{t}"                # E1.1 guard
        assert await r(f"Palm{t}") == f"Palm{t}"                          # classified exact
        assert await r(f"sea {t}") == f"Ocean {t}"                        # class-less folds
        assert await r(f"sea {t}", "organization") == f"sea {t}"          # guard bites
        assert await r(f"shared{t}") == f"Young Country {t}"              # priority first
        assert await r(f"agetie{t}") == f"Older Org {t}"                  # then age
        assert await r(f"ecole {t}") == f"ecole {t}"                      # no unaccent
        assert await r(f"école {t}") == f"École {t}"                      # lower() folds É
        assert await r("   ") == "   "                                    # empty input

        # the batch answers what the single call answers, name by name
        names = [f"ko{t}", f"FRAG{t}", f"Palm{t}", f"sea {t}", f"nobody{t}",
                 f"shared{t}", "", f"keeper org {t}", f"ko{t}"]
        got = await resolve_keepers(c, names)
        for n in names:
            assert got[n] == await r(n), n


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", MODES)
async def test_merged_away_profiles_follow_to_the_keeper(pg_pool, tag, mode, monkeypatch) -> None:
    """Live, 1,451 profiles carried ``merged_into``
    with ``gc_status`` 'dormant', and the lookup read only ``gc_status`` — so a
    tombstone was elected for its own name. Now it is followed to its keeper,
    identically on the scan and the key table (and in the batch)."""
    monkeypatch.setenv(er.ALIAS_LOOKUP_ENV, mode)
    t = tag
    async with pg_pool.acquire() as c:
        keeper = await _seed(c, f"Keeper Country {t}", "country", created_at="2025-01-01")
        tomb = await _seed(c, f"Tomb {t}", "organization", aliases=[f"tombalias{t}"],
                           gc="dormant", created_at="2020-01-01")
        mid = await _seed(c, f"Middle {t}", "organization", gc="dormant")
        far = await _seed(c, f"Far Tomb {t}", "entity", aliases=[f"faralias{t}"], gc="dormant")
        to_junk = await _seed(c, f"Junk End {t}", "organization", gc="junk")
        orphan = await _seed(c, f"Orphan {t}", "organization", aliases=[f"orphanalias{t}"], gc="dormant")
        # an older active org that also claims the tombstone's alias
        rival = await _seed(c, f"Rival Org {t}", "organization", aliases=[f"tombalias{t}"],
                            created_at="2024-01-01")
        frag = await _seed(c, f"FRAG{t}", "entity")
        await _seed(c, f"Frag Holder {t}", "location", aliases=[f"FRAG{t}"], gc="dormant",
                    eid=None)
        await c.execute("UPDATE entity_profiles SET merged_into = $2::uuid WHERE id = $1::uuid", tomb, keeper)
        await c.execute("UPDATE entity_profiles SET merged_into = $2::uuid WHERE id = $1::uuid", far, mid)
        await c.execute("UPDATE entity_profiles SET merged_into = $2::uuid WHERE id = $1::uuid", mid, keeper)
        await c.execute("UPDATE entity_profiles SET merged_into = $2::uuid WHERE id = $1::uuid", orphan, to_junk)
        holder = await c.fetchval("SELECT id::text FROM entity_profiles WHERE canonical_name = $1", f"Frag Holder {t}")
        await c.execute("UPDATE entity_profiles SET merged_into = $2::uuid WHERE id = $1::uuid", holder, keeper)
        assert rival and frag

        async def r(name: str, cls: str | None = None) -> str:
            return await resolve_keeper(c, name, entity_class=cls)

        assert await r(f"tomb {t}") == f"Keeper Country {t}"          # exact name of a tombstone
        assert await r(f"Far Tomb {t}") == f"Keeper Country {t}"      # two hops
        assert await r(f"faralias{t}") == f"Keeper Country {t}"       # normalized, via alias, two hops
        # the alias is claimed by a tombstone (-> a COUNTRY keeper) and an active
        # org: the keeper's own priority ranks them
        assert await r(f"tombalias{t}") == f"Keeper Country {t}"
        assert await r(f"orphan {t}") == f"orphan {t}"                # chain ends at junk: no keeper
        assert await r(f"orphanalias{t}") == f"orphanalias{t}"
        assert await r(f"Middle {t}") == f"Keeper Country {t}"
        # E1.1: the fragment's exact-alias override sees the holder's KEEPER
        assert await r(f"FRAG{t}") == f"Keeper Country {t}"
        names = [f"tomb {t}", f"Far Tomb {t}", f"faralias{t}", f"tombalias{t}", f"orphan {t}",
                 f"FRAG{t}", f"Middle {t}"]
        got = await resolve_keepers(c, names)
        for n in names:
            assert got[n] == await r(n), n


@pytest.mark.asyncio
async def test_table_tie_break_is_deterministic_by_id(pg_pool, tag, monkeypatch) -> None:
    """Equal (priority, created_at): the table elects the lowest id."""
    monkeypatch.setenv(er.ALIAS_LOOKUP_ENV, "table")
    lo, hi = sorted(str(uuid.uuid4()) for _ in range(2))
    async with pg_pool.acquire() as c:
        await _seed(c, f"Twin B {tag}", aliases=[f"twin{tag}"],
                    created_at="2024-05-05", eid=hi)
        await _seed(c, f"Twin A {tag}", aliases=[f"twin{tag}"],
                    created_at="2024-05-05", eid=lo)
        for _ in range(3):
            row = await c.fetchrow(er._ALIAS_TABLE_SQL, f"twin{tag}")
            assert str(row["id"]) == lo


# ---------------------------------------------------------------------------
# Drift: every way aliases change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_drift_through_alias_merge_split_rename_retire_delete(
        pg_pool, tag, monkeypatch) -> None:
    monkeypatch.setenv(er.ALIAS_LOOKUP_ENV, "table")
    t = tag
    async with pg_pool.acquire() as c:
        keeper = await _seed(c, f"Keeper {t}", aliases=[f"k1{t}"])
        loser = await _seed(c, f"Loser {t}", "entity", aliases=[f"l1{t}"])
        ids = [keeper, loser]
        assert await _drift(c, ids) == 0

        # an alias added (the ingestion _record_provenance shape)
        await c.execute(
            "UPDATE entity_profiles SET data = jsonb_set(data, '{merged_aliases}', "
            "data->'merged_aliases' || $2::jsonb) WHERE id = $1::uuid",
            keeper, json.dumps([f"k2{t}"]))
        assert await resolve_keeper(c, f"k2{t}") == f"Keeper {t}"
        assert await _drift(c, ids) == 0

        # a merge: loser retired, its name + aliases moved onto the keeper
        async with c.transaction():
            await c.execute(
                "UPDATE entity_profiles SET data = data || '{\"gc_status\":\"merged\"}'::jsonb, "
                "merged_into = $2::uuid WHERE id = $1::uuid", loser, keeper)
            await c.execute(
                "UPDATE entity_profiles SET data = jsonb_set(data, '{merged_aliases}', "
                "data->'merged_aliases' || $2::jsonb) WHERE id = $1::uuid",
                keeper, json.dumps([f"Loser {t}", f"l1{t}"]))
        assert await resolve_keeper(c, f"l1{t}") == f"Keeper {t}"
        assert await _drift(c, ids) == 0

        # an alias removed
        await c.execute(
            "UPDATE entity_profiles SET data = jsonb_set(data, '{merged_aliases}', "
            "(data->'merged_aliases') - $2) WHERE id = $1::uuid", keeper, f"k1{t}")
        assert await resolve_keeper(c, f"k1{t}") == f"k1{t}"
        assert await _drift(c, ids) == 0

        # a split: the alias leaves the keeper, a new profile takes the name
        async with c.transaction():
            await c.execute(
                "UPDATE entity_profiles SET data = jsonb_set(data, '{merged_aliases}', "
                "(data->'merged_aliases') - $2) WHERE id = $1::uuid", keeper, f"k2{t}")
            split = await _seed(c, f"Split Off {t}", aliases=[f"k2{t}"])
        ids.append(split)
        assert await resolve_keeper(c, f"k2{t}") == f"Split Off {t}"
        assert await _drift(c, ids) == 0

        # a rename
        await c.execute("UPDATE entity_profiles SET canonical_name = $2 WHERE id = $1::uuid",
                        split, f"Renamed {t}")
        assert await resolve_keeper(c, f"the renamed {t}") == f"Renamed {t}"
        assert await _drift(c, ids) == 0

        # a retirement (gc junk) needs no key change: filtered at read
        await c.execute(
            "UPDATE entity_profiles SET data = data || '{\"gc_status\":\"junk\"}'::jsonb "
            "WHERE id = $1::uuid", split)
        assert await resolve_keeper(c, f"k2{t}") == f"k2{t}"
        assert await _drift(c, ids) == 0

        # a delete cascades
        n_before = await c.fetchval(
            "SELECT count(*) FROM entity_alias_keys WHERE entity_id = $1::uuid", split)
        assert n_before > 0
        await c.execute("DELETE FROM entity_profiles WHERE id = $1::uuid", split)
        assert await c.fetchval(
            "SELECT count(*) FROM entity_alias_keys WHERE entity_id = $1::uuid", split) == 0
        assert await _drift(c, [keeper, loser]) == 0


@pytest.mark.asyncio
async def test_ingestion_upsert_conflict_branch_does_not_rekey(pg_pool, tag) -> None:
    async with pg_pool.acquire() as c:
        eid = await _seed(c, f"Upsert {tag}", aliases=[f"u{tag}"])
        ctid = await c.fetchval(
            "SELECT array_agg(ctid::text ORDER BY key) FROM entity_alias_keys "
            "WHERE entity_id = $1::uuid", eid)
        await c.execute(
            """
            INSERT INTO entity_profiles (canonical_name, entity_type, entity_class, data)
            VALUES ($1, 'organization', 'organization', '{}'::jsonb)
            ON CONFLICT (lower(canonical_name), entity_class) DO UPDATE
                SET last_event_link_at = now()
            """, f"Upsert {tag}")
        after = await c.fetchval(
            "SELECT array_agg(ctid::text ORDER BY key) FROM entity_alias_keys "
            "WHERE entity_id = $1::uuid", eid)
    assert ctid == after  # same physical rows: nothing rewritten


# ---------------------------------------------------------------------------
# Backfill + verifier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_dry_run_apply_rerun_and_resume(
        pg_pool, migrated_pg, tag, tmp_path) -> None:
    import argparse
    import importlib.util

    # ids at the top of the uuid space, so the walk below covers only them
    floor = "fffffffe-0000-0000-0000-000000000000"
    async with pg_pool.acquire() as c:
        ids = [await _seed(c, f"Bf{i} {tag}", aliases=[f"bfa{i}{tag}"],
                           eid="fffffffe-" + str(uuid.uuid4())[9:])
               for i in range(3)]
        # simulate profiles that predate the trigger: their keys are gone
        await c.execute("DELETE FROM entity_alias_keys WHERE entity_id = ANY($1::uuid[])", ids)
        assert await _drift(c, ids) > 0

        async def walk(apply: bool, batch: int = 500) -> tuple[int, int]:
            ins = dele = 0
            after = floor
            while True:
                r = await backfill_batch(c, after_id=after, batch_size=batch, apply=apply)
                if r.profiles == 0:
                    return ins, dele
                ins, dele, after = ins + r.inserted, dele + r.deleted, r.last_id

        ins, dele = await walk(apply=False)
        assert ins == 3 * 3 and dele == 0          # norm(name), norm(alias), alias_lower
        assert await _drift(c, ids) == 9            # the dry run wrote nothing
        ins, dele = await walk(apply=True)
        assert (ins, dele) == (9, 0)
        assert await _drift(c, ids) == 0
        assert await walk(apply=True) == (0, 0)     # a re-run writes nothing

        # an extra (stale) key is deleted by the backfill
        await c.execute(
            "INSERT INTO entity_alias_keys (key_kind, key, entity_id) VALUES "
            "('norm', $2, $1::uuid)", ids[0], f"stale{tag}")
        assert await walk(apply=True) == (0, 1)

    # the script: dry run by default (no cursor), --apply resumes from its cursor
    spec = importlib.util.spec_from_file_location(
        "eak_backfill", _SCRIPTS / "entity_alias_keys_backfill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    dsn = migrated_pg.dsn
    cur = tmp_path / "cursor.json"
    assert await mod.run(argparse.Namespace(
        apply=False, batch_size=1, cursor_file=str(cur), restart=False,
        max_batches=2, log_every=1, statement_timeout_ms=60000), dsn) == 0
    assert not cur.exists()
    ns = dict(apply=True, batch_size=1, cursor_file=str(cur), restart=False,
              max_batches=2, log_every=1, statement_timeout_ms=60000)
    Namespace = argparse.Namespace
    await mod.run(Namespace(**ns), dsn)
    first = json.loads(cur.read_text())
    assert first["batches"] == 2 and first["profiles"] == 2
    await mod.run(Namespace(**ns), dsn)
    second = json.loads(cur.read_text())
    assert second["last_id"] > first["last_id"]   # resumed after the cursor


# ---------------------------------------------------------------------------
# EXPLAIN — the probes are index lookups
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("sql_name,arg", [
    ("_EXACT_ALIAS_TABLE_SQL", "someone"), ("_ALIAS_TABLE_SQL", "someone"),
])
async def test_explain_uses_the_key_index(pg_pool, sql_name, arg) -> None:
    sql = getattr(er, sql_name)
    async with pg_pool.acquire() as c:
        async with c.transaction():
            # the test DB is tiny; disable the seq-scan escape so the plan shows
            # whether the predicate CAN use the index (the scan's predicate can't)
            await c.execute("SET LOCAL enable_seqscan = off")
            plan = "\n".join(r[0] for r in await c.fetch(
                "EXPLAIN " + sql.replace("$1", "'" + arg + "'")))
    assert "entity_alias_keys_pkey" in plan
    assert "Seq Scan" not in plan


# ---------------------------------------------------------------------------
# scan = byte-identical; shadow serves the scan; recording connection
# ---------------------------------------------------------------------------

_PINNED = {  # sha256 of the statement text (the merged_into follow rule)
    "_EXACT_SQL": "54934a741ae86a10f4708e6d784ea81be936e8dee9683c28709f97ecda33a3e2",
    "_EXACT_ALIAS_SQL": "7f71745f2634159925ec274f9a6a58ea507fa393864ccbf08de9d8e36d950ab6",
    "_ALIAS_SQL": "0eab139bfea1ffd5ac1d6f91d5ae288575ea4d36fde76bf72df31adc78d29937",
}
_INGEST_FALLBACK_SHA = "e73d9e8605a65e93e4033eb996c1c1fcd0498d7857f2cd12c4059ce6358f065c"


def test_every_election_statement_follows_merged_into() -> None:
    """The rule is one fragment, in every statement on both paths and
    in the ingestion resolver's own copies."""
    for name in ("_EXACT_SQL", "_EXACT_ALIAS_SQL", "_ALIAS_SQL", "_EXACT_ALIAS_TABLE_SQL",
                 "_ALIAS_TABLE_SQL", "_EXACT_BATCH_SQL", "_EXACT_ALIAS_BATCH_SQL", "_ALIAS_BATCH_SQL"):
        sql = getattr(er, name)
        assert er.FOLLOW_SQL in sql and er.ACTIVE_K in sql and er.MATCHABLE_P in sql, name
    from legba.data.analysts.deterministic_handlers import entity_resolution as ing
    src = inspect.getsource(ing._resolve_batch)
    assert src.count("{FOLLOW_SQL}") >= 2 and src.count("{ACTIVE_K}") >= 2


def test_scan_statements_are_pinned() -> None:
    for name, sha in _PINNED.items():
        assert hashlib.sha256(getattr(er, name).encode()).hexdigest() == sha, name
    from legba.data.analysts.deterministic_handlers import entity_resolution as ing
    src = inspect.getsource(ing._resolve_batch)
    i = src.index("fetch_normalized_keeper(\n")
    j = src.index('f"""', i) + 4
    k = src.index('"""', j)
    assert hashlib.sha256(src[j:k].encode()).hexdigest() == _INGEST_FALLBACK_SHA


class _Rec:
    """A recording connection: answers by statement identity."""

    def __init__(self, answers: dict[str, Any]):
        self.answers = answers
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        for key, val in self.answers.items():
            if sql == getattr(er, key):
                return val
        return None

    async def fetch(self, sql: str, *args: Any) -> list:
        self.calls.append((sql, args))
        return []


_FRAG = {"id": uuid.UUID(int=1), "entity_class": "entity", "canonical_name": "F", "prio": 4}
_ORG = {"id": uuid.UUID(int=2), "entity_class": "organization",
        "canonical_name": "Org", "prio": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("env", [None, "scan", "SCAN", "bogus"])
async def test_scan_sends_exactly_the_old_sequence(env, monkeypatch) -> None:
    if env is None:
        monkeypatch.delenv(er.ALIAS_LOOKUP_ENV, raising=False)
    else:
        monkeypatch.setenv(er.ALIAS_LOOKUP_ENV, env)
    rec = _Rec({"_EXACT_SQL": _FRAG, "_EXACT_ALIAS_SQL": _ORG})
    assert await resolve_keeper(rec, " F ") == "Org"
    assert rec.calls == [(er._EXACT_SQL, ("F",)), (er._EXACT_ALIAS_SQL, ("F",))]
    rec = _Rec({"_ALIAS_SQL": _ORG})
    assert await resolve_keeper(rec, "The  Org") == "Org"
    assert rec.calls == [(er._EXACT_SQL, ("The  Org",)), (er._ALIAS_SQL, ("org",))]
    rec = _Rec({"_ALIAS_SQL": _ORG})
    got = await resolve_keepers(rec, ["The  Org", "The  Org"])
    assert got == {"The  Org": "Org"}
    assert [c[0] for c in rec.calls] == [er._EXACT_SQL, er._ALIAS_SQL] * 2


@pytest.mark.asyncio
async def test_shadow_serves_the_scan_and_counts(monkeypatch, caplog) -> None:
    monkeypatch.setenv(er.ALIAS_LOOKUP_ENV, "shadow")
    er.SHADOW_STATS.clear()
    rec = _Rec({"_ALIAS_SQL": _ORG, "_ALIAS_TABLE_SQL": None})
    with caplog.at_level(logging.WARNING, logger=er.__name__):
        assert await resolve_keeper(rec, "the org") == "Org"   # the scan's answer
    assert [c[0] for c in rec.calls] == [er._EXACT_SQL, er._ALIAS_SQL, er._ALIAS_TABLE_SQL]
    assert er.SHADOW_STATS["norm.missing_in_table"] == 1
    assert er.SHADOW_STATS["norm.lookups"] == 1
    assert "shadow_disagree probe=norm class=missing_in_table" in caplog.text
    assert "org" not in caplog.text.lower().replace("probe=norm", "")  # no names

    class _Boom(_Rec):
        async def fetchrow(self, sql, *args):
            if sql == er._ALIAS_TABLE_SQL:
                raise RuntimeError("no table")
            return await super().fetchrow(sql, *args)

    boom = _Boom({"_ALIAS_SQL": _ORG})
    assert await resolve_keeper(boom, "the org") == "Org"
    assert er.SHADOW_STATS["norm.table_error"] == 1
    er.SHADOW_STATS.clear()


@pytest.mark.asyncio
async def test_long_probe_always_scans(monkeypatch) -> None:
    monkeypatch.setenv(er.ALIAS_LOOKUP_ENV, "table")
    long_name = "x" * (er._TABLE_PROBE_MAX_BYTES + 1)
    rec = _Rec({})
    assert await resolve_keeper(rec, long_name) == long_name
    assert [c[0] for c in rec.calls] == [er._EXACT_SQL, er._ALIAS_SQL]
