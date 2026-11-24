# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-5 — ``scripts/prune_retired_actor_state.py`` on the isolated test stack.

A retired ``actor_state`` row goes only when it is retired, superseded by a
head a live actor runs, has no reminder (sidecar GET 404), and is older than
the window. Pinned here on a real database:

* each keep-rule keeps its row, with that reason first; only the rows every
  rule lets go are candidates; a row that registers no reminder is not probed;
* the dry run (``main`` without ``--apply``) writes nothing;
* ``--apply`` backs every candidate up WHOLE first (and refuses to overwrite a
  backup), deletes exactly those rows, and a backup line restores its row;
* a row written between the plan and the delete is not deleted.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.runtime.state import SCHEMA as ACTOR_STATE_SCHEMA

REPO = pathlib.Path(__file__).resolve().parents[2]


def _script():
    spec = importlib.util.spec_from_file_location(
        "prune_retired_actor_state", REPO / "scripts" / "prune_retired_actor_state.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod  # @dataclass resolves its module by name
    spec.loader.exec_module(mod)
    return mod


P = _script()


@pytest_asyncio.fixture
async def world(migrated_pg: PostgresConfig):
    """A private slice of descriptors + actor_state rows (unique prefix)."""
    pfx = f"p5prune_{uuid4().hex[:8]}"
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=3)
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=60)
    async with pool.acquire() as c:
        await c.execute(ACTOR_STATE_SCHEMA)

        async def desc(table: str, did: str, version: str, head: bool) -> None:
            extra = ", kind" if table == "analyst_descriptors" else ""
            extra_v = ", 'llm'" if table == "analyst_descriptors" else ""
            await c.execute(
                f"INSERT INTO public.{table} (descriptor_id, version, schema_uri, is_head, owner, name, body{extra}) "
                f"VALUES ($1, $2, 'iglu:test/x/jsonschema/1-0-0', $3, 'test', $1, '{{}}'::jsonb{extra_v})",
                did, version, head,
            )

        async def actor(aid: str, kind: str, did: str, ver: str, life: str, upd: datetime,
                        cursors: dict | None = None) -> None:
            await c.execute(
                "INSERT INTO public.actor_state (actor_id, actor_kind, descriptor_id, descriptor_version, "
                "lifecycle, source_cursors, extras, updated_at) VALUES ($1,$2,$3,$4,$5,$6::jsonb,'{}'::jsonb,$7)",
                aid, kind, did, ver, life, json.dumps(cursors or {}), upd,
            )

        a, d, t, s = f"{pfx}_a", f"{pfx}_d", f"{pfx}_t", f"{pfx}_s"
        # analyst `a`: head v3 runs live; v1 old (prune), v2 recent (keep),
        # v0 old but its reminder is present (keep), vx old and the probe
        # fails (keep), and a retired row AT the head version (keep).
        for ver, head in (("v0", False), ("v1", False), ("v2", False), ("vx", False), ("v3", True)):
            await desc("analyst_descriptors", a, ver, head)
        await actor(f"analyst::{a}::v3", "analyst", a, "v3", "active", now)
        await actor(f"analyst::{a}::v1", "analyst", a, "v1", "retired", old)
        await actor(f"analyst::{a}::v2", "analyst", a, "v2", "retired", now - timedelta(days=5))
        await actor(f"analyst::{a}::v0", "analyst", a, "v0", "retired", old)
        await actor(f"analyst::{a}::vx", "analyst", a, "vx", "retired", old)
        await actor(f"analyst::{a}::v3r", "analyst", a, "v3", "retired", old)
        # analyst `d`: a head no live actor runs.
        await desc("analyst_descriptors", d, "v1", False)
        await desc("analyst_descriptors", d, "v2", True)
        await actor(f"analyst::{d}::v1", "analyst", d, "v1", "retired", old)
        # source `s`: no descriptor at all.
        await actor(f"source::{s}::v1", "source", s, "v1", "retired", old, {s: {}})
        # target `t`: head v2 live; v1 old, registers no reminder (prune).
        await desc("target_descriptors", t, "v1", False)
        await desc("target_descriptors", t, "v2", True)
        await actor(f"target::{t}::v2", "target", t, "v2", "active", now)
        await actor(f"target::{t}::v1", "target", t, "v1", "retired", old)
    try:
        yield {"pool": pool, "pfx": pfx, "a": a, "d": d, "t": t, "s": s, "cfg": migrated_pg}
    finally:
        async with pool.acquire() as c:
            await c.execute("DELETE FROM public.actor_state WHERE descriptor_id LIKE $1", pfx + "%")
            for table in ("analyst_descriptors", "target_descriptors", "source_descriptors"):
                await c.execute(f"DELETE FROM public.{table} WHERE descriptor_id LIKE $1", pfx + "%")
        await pool.close()


class Probe:
    """Fake sidecar: present for v0, raises for vx, absent otherwise."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(self, actor_type: str, actor_id: str, name: str) -> bool:
        self.calls.append((actor_type, actor_id, name))
        if actor_id.endswith("::v0"):
            return True
        if actor_id.endswith("::vx"):
            raise RuntimeError("sidecar 500")
        return False


async def _plan(world, probe=None) -> dict[str, object]:
    async with world["pool"].acquire() as c:
        rows = await P.fetch_retired(c, older_than_days=30)
    mine = [r for r in rows if r["descriptor_id"].startswith(world["pfx"])]
    verdicts = await P.plan(mine, probe or Probe())
    return {v.actor_id: v for v in verdicts}


@pytest.mark.asyncio
async def test_each_rule_keeps_its_row_and_only_all_clear_rows_go(world) -> None:
    probe = Probe()
    v = await _plan(world, probe)
    a, d, t, s = world["a"], world["d"], world["t"], world["s"]
    assert {k for k, x in v.items() if x.prune} == {f"analyst::{a}::v1", f"target::{t}::v1"}
    first = {k: x.reasons[0] for k, x in v.items() if not x.prune}
    assert first == {
        f"analyst::{a}::v2": P.KEEP_RECENT,
        f"analyst::{a}::v0": P.KEEP_REMINDER,
        f"analyst::{a}::vx": P.KEEP_REMINDER_UNKNOWN,
        f"analyst::{a}::v3r": P.KEEP_IS_HEAD,
        f"analyst::{d}::v1": P.KEEP_NO_LIVE,
        f"source::{s}::v1": P.KEEP_NO_HEAD,
    }
    # live rows are never judged; the probe ran only for rows every other rule
    # let go, and a target (registers no reminder) was never probed.
    assert f"analyst::{a}::v3" not in v and f"target::{t}::v2" not in v
    assert sorted(c[1] for c in probe.calls) == sorted(
        [f"analyst::{a}::v1", f"analyst::{a}::v0", f"analyst::{a}::vx"])
    assert all(c[0] == "AnalystActor" and c[2] == "run_cadence" for c in probe.calls)
    assert "no reminder (registers none)" in v[f"target::{t}::v1"].reasons


@pytest.mark.asyncio
async def test_dry_run_main_writes_nothing(world, monkeypatch, capsys) -> None:
    cfg = world["cfg"]
    for k, val in (("HOST", cfg.host), ("PORT", str(cfg.port)), ("USER", cfg.user),
                   ("PASSWORD", cfg.password), ("DB", cfg.database)):
        monkeypatch.setenv(f"LEGBA_DATA_PG_{k}", val)
    monkeypatch.setattr(P, "build_sidecar_probe", lambda **_: Probe())

    async def _no_apply(*_a, **_k):
        raise AssertionError("dry run must not apply")

    monkeypatch.setattr(P, "apply", _no_apply)
    async with world["pool"].acquire() as c:
        before = await c.fetchval("SELECT count(*) FROM public.actor_state WHERE descriptor_id LIKE $1",
                                  world["pfx"] + "%")
    assert await P.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "dry run: nothing written" in out and f"analyst::{world['a']}::v1" in out
    async with world["pool"].acquire() as c:
        after = await c.fetchval("SELECT count(*) FROM public.actor_state WHERE descriptor_id LIKE $1",
                                 world["pfx"] + "%")
    assert before == after == 10


@pytest.mark.asyncio
async def test_apply_backs_up_whole_rows_then_deletes_exactly_them(world, tmp_path) -> None:
    v = await _plan(world)
    mine = list(v.values())
    path = str(tmp_path / "backup.jsonl")
    async with world["pool"].acquire() as c:
        full_before = {r["actor_id"]: dict(r) for r in await c.fetch(
            "SELECT * FROM public.actor_state WHERE descriptor_id LIKE $1", world["pfx"] + "%")}
        backed, deleted = await P.apply(c, mine, path)
        left = {r["actor_id"] for r in await c.fetch(
            "SELECT actor_id FROM public.actor_state WHERE descriptor_id LIKE $1", world["pfx"] + "%")}
    gone = {k for k, x in v.items() if x.prune}
    assert backed == deleted == len(gone) == 2
    assert left == set(full_before) - gone
    lines = [json.loads(x) for x in pathlib.Path(path).read_text().splitlines()]
    assert {x["actor_id"] for x in lines} == gone
    assert set(lines[0]) == set(full_before[lines[0]["actor_id"]])  # every column
    with pytest.raises(FileExistsError):  # a backup is never overwritten
        async with world["pool"].acquire() as c:
            await P.backup_rows(c, mine, path)
    # a backup line restores its row
    async with world["pool"].acquire() as c:
        for line in pathlib.Path(path).read_text().splitlines():
            await c.execute("INSERT INTO public.actor_state SELECT * FROM "
                            "json_populate_record(NULL::public.actor_state, $1::json)", line)
        restored = {r["actor_id"]: dict(r) for r in await c.fetch(
            "SELECT * FROM public.actor_state WHERE actor_id = ANY($1::text[])", sorted(gone))}
    assert restored == {k: full_before[k] for k in gone}


@pytest.mark.asyncio
async def test_a_row_written_after_the_plan_is_not_deleted(world, tmp_path) -> None:
    v = await _plan(world)
    victim = f"analyst::{world['a']}::v1"
    async with world["pool"].acquire() as c:
        await c.execute("UPDATE public.actor_state SET updated_at = now() WHERE actor_id = $1", victim)
        _backed, deleted = await P.apply(c, list(v.values()), str(tmp_path / "b.jsonl"))
        still = await c.fetchval("SELECT count(*) FROM public.actor_state WHERE actor_id = $1", victim)
    assert deleted == 1 and still == 1


@pytest.mark.asyncio
async def test_nothing_to_prune_writes_no_backup(world, tmp_path) -> None:
    v = await _plan(world)
    keep_only = [x for x in v.values() if not x.prune]
    path = tmp_path / "none.jsonl"
    async with world["pool"].acquire() as c:
        assert await P.apply(c, keep_only, str(path)) == (0, 0)
    assert not path.exists()
