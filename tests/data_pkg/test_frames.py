# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W7 FRAME-1 — the ``frame`` vocabulary family replaces the ``region_`` prefix.

``legba.data.frames`` resolves a composition's subject groups from registered
``frame`` rows (parent link + member rule) and falls back to the legacy
id-prefix / id-as-tag convention when the family is not loadable.
``LEGBA_FRAME_FAMILY`` (default OFF) routes the composition slice's roster and
member reads through it.

  * the pure half: the catalog, the parent walk, the fallback, the member rule;
  * FLAG OFF IS BYTE-IDENTICAL: on the live topology (the committed roster
    fixture + the tree's target bodies) the flag-off slice issues exactly the
    legacy statements and NO vocabulary read, and its stamped world slice is
    the flag-on one, hash for hash;
  * the real driver: the walker seeds the family into a fresh database next to
    the live topology, and every frame's member set is equal under both
    resolvers — then the family is broken and emptied and the fallback still
    equals the legacy read.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data import frames
from legba.data.analysts import composition_slice as slice_
from legba.data.schemas.vocabulary import VocabularyEntry

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
ROSTER = REPO_ROOT / "tests" / "fixtures" / "bringup" / "live_head_roster_2026-10-04.json"
DESCRIPTORS = REPO_ROOT / "descriptors"

_SCRIPTS_DIR = REPO_ROOT / "scripts"
_HAD_PG_DB = "LEGBA_DATA_PG_DB" in os.environ
sys.path.insert(0, str(_SCRIPTS_DIR))
try:
    import bringup_register_descriptors as walker  # noqa: E402
finally:
    if not _HAD_PG_DB:
        os.environ.pop("LEGBA_DATA_PG_DB", None)

_NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


def _e(value: str, parent: str | None = None, aliases=(), notes: str | None = None):
    return VocabularyEntry(
        family="frame", value=value, schema_uri="legba/vocabulary/1.0.0",
        introduced=_NOW, parent=parent, aliases=list(aliases), notes=notes,
    )


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv(frames.FRAME_FAMILY_ENV, raising=False)


# ---------------------------------------------------------------------------
# The live topology, from committed files only
# ---------------------------------------------------------------------------


def _live_targets() -> list[dict[str, Any]]:
    """Every non-retired target head of the roster fixture, at its LIVE state,
    with the tree's name and scope tags (the walker registers these bodies)."""
    heads = json.loads(ROSTER.read_text())["heads"]
    states = {h["id"]: h["state"] for h in heads
              if h["family"] == "target" and h["state"] != "retired"}
    out = []
    for p in sorted(DESCRIPTORS.glob("target_*.yaml")):
        body = yaml.safe_load(p.read_text())
        did = body["identity"]["id"]
        if did in states:
            out.append({"descriptor_id": did, "name": body["identity"].get("name") or did,
                        "state": states[did],
                        "tags": list((body.get("scope") or {}).get("tags") or [])})
    assert {t["descriptor_id"] for t in out} == set(states), "a live target has no tree file"
    return out


def _seed_rows() -> list[dict[str, Any]]:
    return [
        {"family": s.family, "value": s.value, "schema_uri": "iglu:legba/vocabulary/jsonschema/1-0-0",
         "introduced": _NOW, "deprecated": None, "notes": s.notes,
         "aliases": list(s.aliases), "parent": s.parent}
        for s in walker.load_vocabulary_seeds() if s.family == frames.FRAME_FAMILY
    ]


class _TopologyConn:
    """Answers the slice's and the leaf's statements from an in-memory topology,
    and records every statement it is asked (by identity of the SQL constant)."""

    def __init__(self, targets, vocab_rows):
        self.targets = targets
        self.vocab_rows = vocab_rows
        self.calls: list[tuple[str, tuple]] = []

    def _active(self):
        return [t for t in self.targets if t["state"] == "active"]

    @staticmethod
    def _rows(ts, *cols):
        return sorted(({c: t[c] for c in cols} for t in ts), key=lambda r: r["descriptor_id"])

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        if sql is slice_._REGION_MEMBERS_SQL:
            return self._rows([t for t in self._active() if args[0] in t["tags"]
                               and t["descriptor_id"] != args[0]], "descriptor_id")
        if sql in (slice_._REGION_MEMBER_ROSTER_SQL, frames._LEGACY_MEMBER_ROSTER_SQL):
            return self._rows([t for t in self._active() if args[0] in t["tags"]
                               and t["descriptor_id"] != args[0]], "descriptor_id", "name")
        if sql in (slice_._REGION_ROSTER_SQL, frames._LEGACY_ROSTER_SQL):
            return self._rows([t for t in self._active() if args[0] in t["tags"]],
                              "descriptor_id", "name")
        if sql is frames._MEMBER_ROSTER_SQL:
            return self._rows([t for t in self._active() if set(args[0]) & set(t["tags"])
                               and t["descriptor_id"] not in args[1]], "descriptor_id", "name")
        if sql is frames._FRAME_HEADS_SQL:
            return self._rows([t for t in self.targets if t["descriptor_id"] in args[0]],
                              "descriptor_id", "name", "state")
        if sql is frames._FRAME_ROWS_SQL:
            return [r for r in self.vocab_rows if r["family"] == args[0]]
        if sql is slice_._UNIT_NAME_SQL:
            return self._rows([t for t in self.targets if t["descriptor_id"] in args[0]],
                              "descriptor_id", "name")
        if sql in (slice_._COUNTRY_ASSESSMENT_BY_SPINE_SQL, slice_._COUNTRY_ASSESSMENT_BY_TARGET_SQL):
            return []
        raise AssertionError(f"unexpected statement: {sql[:80]!r}")

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        assert sql is slice_._REGION_NAME_SQL
        hit = [t for t in self.targets if t["descriptor_id"] == args[0]]
        return {"name": hit[0]["name"]} if hit else None


# ---------------------------------------------------------------------------
# The pure half
# ---------------------------------------------------------------------------


def _catalog() -> frames.FrameCatalog:
    return frames.FrameCatalog([
        _e("region", notes="Regions"),
        _e("region_a", "frame:region", ["tag:region_a"], "Region — A"),
        _e("region_b", "frame:region", ["tag:region_b"]),
        _e("sub_b1", "frame:region_b", ["tag:sub_b1"]),
        _e("thematic"),
        _e("lane_x", "frame:thematic", ["analyst:lane_x"]),
    ])


def test_composite_frame_reads_the_registered_entry():
    cat = _catalog()
    f = frames.composite_frame("region_a", cat)
    assert f == frames.Frame("region_a", "Region — A", "frame:region", ("tag:region_a",))
    assert f.member_tags == ("region_a",) and f.member_analysts == () and f.parent_id == "region"
    assert frames.composite_frame("region_b", cat).label == "region_b"  # no notes ⇒ the id
    lane = frames.composite_frame("lane_x", cat)
    assert lane.member_analysts == ("lane_x",) and lane.member_tags == ()


def test_a_loaded_catalog_is_authoritative_over_the_legacy_rule():
    legacy = frames.LegacyFrames(id_prefix="region_", kind="region")
    assert frames.composite_frame("region_zz", _catalog(), legacy=legacy) is None


def test_the_legacy_rule_is_the_fallback_when_the_family_is_not_loaded():
    legacy = frames.LegacyFrames(id_prefix="region_", kind="region")
    for cat in (None, frames.FrameCatalog(()), frames.FrameCatalog.unloaded("family_unknown")):
        f = frames.composite_frame("region_zz", cat, legacy=legacy)
        assert f == frames.Frame("region_zz", "region_zz", "frame:region", ("tag:region_zz",))
        assert frames.composite_frame("country_g20_us", cat, legacy=legacy) is None
        assert frames.composite_frame("region_zz", cat) is None  # no legacy, no frame


def test_the_parent_walk_gives_subtrees_and_kind_rosters():
    cat = _catalog()
    assert cat.ancestors("sub_b1") == ["region_b", "region"]
    assert [f.id for f in cat.descendants("region")] == ["region_a", "region_b", "sub_b1"]
    assert [f.id for f in cat.subtree("region_b")] == ["region_b", "sub_b1"]
    assert [f.id for f in cat.groups("region")] == ["region_a", "region_b", "sub_b1"]
    assert [f.id for f in cat.groups("thematic")] == ["lane_x"]
    assert cat.groups("region_a") == []


def test_a_cyclic_parent_chain_terminates():
    cat = frames.FrameCatalog([_e("a", "frame:b", ["tag:a"]), _e("b", "frame:a", ["tag:b"])])
    assert cat.ancestors("a") == ["b"] and [f.id for f in cat.descendants("a")] == ["b"]


async def test_members_union_the_subtree_and_exclude_every_subtree_frame():
    targets = [
        {"descriptor_id": "region_b", "name": "B", "state": "active", "tags": ["region", "region_b"]},
        {"descriptor_id": "sub_b1", "name": "B1", "state": "active", "tags": ["region_b", "sub_b1"]},
        {"descriptor_id": "d1", "name": "D1", "state": "active", "tags": ["region_b"]},
        {"descriptor_id": "d2", "name": "D2", "state": "active", "tags": ["sub_b1"]},
        {"descriptor_id": "d3", "name": "D3", "state": "draft", "tags": ["sub_b1"]},
        {"descriptor_id": "d4", "name": "D4", "state": "active", "tags": ["region_a"]},
    ]
    conn = _TopologyConn(targets, [])
    cat = _catalog()
    assert await frames.resolve_members("region_b", conn, catalog=cat) == ["d1", "d2"]
    assert await frames.resolve_members("sub_b1", conn, catalog=cat) == ["d2"]
    assert await frames.resolve_members("region", conn, catalog=cat) == ["d1", "d2", "d4"]
    # an analyst lane binds no targets, an unregistered id has no members — no query
    n = len(conn.calls)
    assert await frames.resolve_members("lane_x", conn, catalog=cat) == []
    assert await frames.resolve_members("region_zz", conn, catalog=cat) == []
    assert len(conn.calls) == n


async def test_the_frame_roster_skips_an_inactive_head_and_names_a_headless_frame():
    targets = [
        {"descriptor_id": "region_a", "name": "Region — A (head)", "state": "active", "tags": []},
        {"descriptor_id": "region_b", "name": "B", "state": "draft", "tags": []},
    ]
    conn = _TopologyConn(targets, [])
    roster = await frames.resolve_frame_roster("region", conn, catalog=_catalog())
    assert roster == [
        {"frame_id": "region_a", "frame_name": "Region — A (head)"},
        {"frame_id": "sub_b1", "frame_name": "sub_b1"},
    ]


@pytest.mark.parametrize("bad", [
    {"aliases": ["region_a"]},             # no selector kind
    {"aliases": ["member:region_a"]},      # unknown selector kind
    {"aliases": ["tag:"]},                 # empty selector
    {"value": "Upper Case"},               # fails the platform value shape
    {"parent": "x:y:z"},                   # fails the parent shape
])
async def test_one_bad_row_leaves_the_whole_family_unloaded(bad):
    rows = _seed_rows()
    rows[1] = {**rows[1], **bad}
    cat = await frames.load_frame_catalog(_TopologyConn([], rows))
    assert cat.status == frames.STATUS_UNLOADED and not cat.loaded and cat.detail


async def test_no_rows_is_empty_and_not_loaded():
    cat = await frames.load_frame_catalog(_TopologyConn([], []))
    assert cat.status == frames.STATUS_EMPTY and not cat.loaded


async def test_the_seed_file_loads_as_the_frame_family():
    cat = await frames.load_frame_catalog(_TopologyConn([], _seed_rows()))
    assert cat.loaded
    assert [f.id for f in cat.groups("region")] == [
        "region_africa", "region_americas", "region_europe", "region_indo_pacific", "region_mena",
    ]
    for f in cat.groups("region"):
        assert f.member_tags == (f.id,) and f.parent_id == "region"


def test_the_fallback_statements_are_the_slices_own():
    assert frames._LEGACY_MEMBER_ROSTER_SQL == slice_._REGION_MEMBER_ROSTER_SQL
    assert frames._LEGACY_ROSTER_SQL == slice_._REGION_ROSTER_SQL
    assert slice_.LEGACY_FRAMES == frames.LegacyFrames(
        id_prefix=slice_.REGION_TARGET_PREFIX, kind=slice_.REGION_FRAME_TAG)


def test_the_flag_parse(monkeypatch):
    assert frames.frame_family_enabled() is False
    for raw, want in (("1", True), ("on", True), ("TRUE", True), ("0", False), ("", False)):
        monkeypatch.setenv(frames.FRAME_FAMILY_ENV, raw)
        assert frames.frame_family_enabled() is want


def test_the_leaf_names_no_exemplar_id():
    text = (REPO_ROOT / "src" / "legba" / "data" / "frames.py").read_text()
    for word in ("region", "country", "g20", "mena", "escalation"):
        assert word not in text.lower(), word


# ---------------------------------------------------------------------------
# Flag off is byte-identical — the live topology, in memory
# ---------------------------------------------------------------------------


_REGION_IDS = ["region_africa", "region_americas", "region_europe", "region_indo_pacific", "region_mena"]


def _basis_rows(targets) -> list[dict[str, Any]]:
    rows = [{"id": f"cc-{t['descriptor_id']}", "analyst_id": "country_composition",
             "target_id": t["descriptor_id"]}
            for t in targets if t["state"] == "active" and t["descriptor_id"].startswith("country_")]
    rows.append({"id": "esc-1", "analyst_id": "escalation_composition", "target_id": None})
    # a desk outside every frame lands in `unassigned` on both paths
    rows.append({"id": "cc-lane", "analyst_id": "country_composition", "target_id": "lane_hormuz"})
    return rows


async def _world_slice(targets, vocab_rows):
    conn = _TopologyConn(targets, vocab_rows)

    async def basis(_conn, **_kw):
        return [dict(r) for r in _basis_rows(targets)]

    rows = await slice_._assemble_world_country_slice(
        conn, region_analyst_ids=["region_composition", "escalation_composition"],
        time_window_hours=336, limit=500, verify_floor=None, basis_reader=basis,
    )
    return rows, conn


def _digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


async def test_flag_off_issues_the_legacy_statements_and_no_vocabulary_read():
    targets = _live_targets()
    conn = _TopologyConn(targets, _seed_rows())
    roster = await slice_._resolve_region_roster(conn)
    for rid in _REGION_IDS:
        await slice_._resolve_region_member_target_ids(conn, rid)
        await slice_.resolve_region_membership(conn, rid)
    _, wconn = await _world_slice(targets, _seed_rows())
    legacy = (slice_._REGION_ROSTER_SQL, slice_._REGION_MEMBERS_SQL,
              slice_._REGION_MEMBER_ROSTER_SQL, slice_._REGION_NAME_SQL)
    assert [r["region_id"] for r in roster] == _REGION_IDS
    assert all(sql in legacy for sql, _ in conn.calls)
    issued = {sql for sql, _ in conn.calls + wconn.calls}
    assert not issued & {frames._FRAME_ROWS_SQL, frames._MEMBER_ROSTER_SQL, frames._FRAME_HEADS_SQL}


async def test_flag_off_and_flag_on_stamp_the_same_world_slice(monkeypatch):
    targets = _live_targets()
    off, _ = await _world_slice(targets, _seed_rows())
    monkeypatch.setenv(frames.FRAME_FAMILY_ENV, "1")
    on, conn = await _world_slice(targets, _seed_rows())
    assert sum(1 for sql, _ in conn.calls if sql is frames._FRAME_ROWS_SQL) == 1  # one family read
    assert [r["id"] for r in on] == [r["id"] for r in off]
    assert _digest(on) == _digest(off)
    cov = {c["region_id"]: c for c in off[0]["_region_coverage"]}
    assert set(cov) == set(_REGION_IDS) | {"unassigned", "thematic:escalation_composition"}
    assert cov["region_indo_pacific"]["input_count"] == 10 and cov["region_mena"]["input_count"] == 4


async def test_flag_on_with_the_family_unloaded_is_the_legacy_read(monkeypatch):
    targets = _live_targets()
    off, _ = await _world_slice(targets, [])
    monkeypatch.setenv(frames.FRAME_FAMILY_ENV, "1")
    bad = _seed_rows()
    bad[0] = {**bad[0], "aliases": ["nope"]}
    for vocab in ([], bad):
        on, conn = await _world_slice(targets, vocab)
        assert _digest(on) == _digest(off)
        assert not {sql for sql, _ in conn.calls} & {frames._MEMBER_ROSTER_SQL, frames._FRAME_HEADS_SQL}


async def test_flag_on_membership_and_roster_equal_flag_off(monkeypatch):
    targets = _live_targets()
    conn = _TopologyConn(targets, _seed_rows())
    off_roster = await slice_._resolve_region_roster(conn)
    off = {rid: (await slice_._resolve_region_member_target_ids(conn, rid),
                 await slice_.resolve_region_membership(conn, rid)) for rid in _REGION_IDS}
    monkeypatch.setenv(frames.FRAME_FAMILY_ENV, "1")
    assert await slice_._resolve_region_roster(conn) == off_roster
    for rid in _REGION_IDS:
        assert (await slice_._resolve_region_member_target_ids(conn, rid),
                await slice_.resolve_region_membership(conn, rid)) == off[rid]


# ---------------------------------------------------------------------------
# The real driver — a fresh database, the walker, the live topology
# ---------------------------------------------------------------------------

_ADMIN_DSN = "postgresql://legba:legba@127.0.0.1:5432/postgres"


@pytest_asyncio.fixture
async def fresh_db():
    from legba.data.config import PostgresConfig
    from legba.data.migrate import apply_primary_migrations

    name = f"legba_frames_{uuid4().hex[:10]}"
    conn = await asyncpg.connect(_ADMIN_DSN)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    cfg = PostgresConfig(host="127.0.0.1", port=5432, user="legba", password="legba", database=name)
    try:
        await apply_primary_migrations(cfg)
        yield cfg
    finally:
        conn = await asyncpg.connect(_ADMIN_DSN)
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{name}' AND pid <> pg_backend_pid()"
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await conn.close()


@pytest.mark.integration
async def test_member_sets_are_equal_per_frame_on_the_live_topology(fresh_db, monkeypatch, capsys):
    from legba.data.postgres import PostgresStore
    from legba.data.registry.descriptor import DescriptorRegistry

    pg = PostgresStore(fresh_db)
    await pg.connect()
    reg = DescriptorRegistry(pg)
    await reg.start()
    try:
        code, decisions = await walker.run(pg, reg, dry_run=False, families={"source", "target"})
        failed = [(d.family, d.descriptor_id, d.detail) for d in decisions if d.action == "failed"]
        assert not failed and code == 0, failed
        targets = _live_targets()

        async with pg.acquire() as conn:
            # the walker reproduces the live target topology (ids AND states)
            got = {r["descriptor_id"]: r["state"] for r in await conn.fetch(
                "SELECT descriptor_id, state FROM target_descriptors WHERE is_head")}
            assert {t["descriptor_id"]: got.get(t["descriptor_id"]) for t in targets} == {
                t["descriptor_id"]: t["state"] for t in targets}
            vocab = {r["value"]: r for r in await conn.fetch(
                "SELECT value, parent, aliases, notes FROM vocabulary_entries WHERE family='frame'")}
            assert set(vocab) == {
                s.value for s in walker.load_vocabulary_seeds() if s.family == frames.FRAME_FAMILY}
            assert vocab["region_mena"]["parent"] == "frame:region"
            assert list(vocab["region_mena"]["aliases"]) == ["tag:region_mena"]

            cat = await frames.load_frame_catalog(conn)
            assert cat.loaded

            # flag OFF — the legacy convention (the authority being replaced)
            off_roster = await slice_._resolve_region_roster(conn)
            off = {r["region_id"]: (await slice_._resolve_region_member_target_ids(conn, r["region_id"]),
                                    await slice_.resolve_region_membership(conn, r["region_id"]))
                   for r in off_roster}
            assert sorted(off) == _REGION_IDS
            assert sum(len(m) for m, _ in off.values()) == 32
            # flag ON — the frame family
            monkeypatch.setenv(frames.FRAME_FAMILY_ENV, "1")
            assert await slice_._resolve_region_roster(conn) == off_roster
            for rid, (members, membership) in off.items():
                assert await slice_._resolve_region_member_target_ids(conn, rid) == members, rid
                assert await slice_.resolve_region_membership(conn, rid) == membership, rid
            # the kind's members are its frames' union (the parent walk)
            assert await frames.resolve_members("region", conn, catalog=cat) == sorted(
                m for ms, _ in off.values() for m in ms)
            # the thematic lane = the world read's declared cross-region lane
            world = yaml.safe_load((DESCRIPTORS / "analyst_world_assessor.yaml").read_text())
            declared = [a for a in slice_.world_admissible_analyst_ids(
                [o["id"] for o in world["subscription"]["other_analysts"]])
                if a != slice_.COUNTRY_COMPOSITION_ANALYST_ID]
            assert [a for f in cat.groups("thematic") for a in f.member_analysts] == declared

            # the fallback on the real driver: broken, then empty ⇒ the legacy read
            await conn.execute(
                "UPDATE vocabulary_entries SET aliases = '{nope}' "
                "WHERE family='frame' AND value='region_mena'")
            assert (await frames.load_frame_catalog(conn)).status == frames.STATUS_UNLOADED
            assert await slice_._resolve_region_roster(conn) == off_roster
            assert await slice_._resolve_region_member_target_ids(conn, "region_mena") == off["region_mena"][0]

        # the walker repairs the row (the tree wins), and a re-run is unchanged
        capsys.readouterr()
        await walker.run(pg, reg, dry_run=False, families={"target"})
        assert "update  frame/region_mena" in capsys.readouterr().out
        await walker.run(pg, reg, dry_run=True, families={"target"})
        out = capsys.readouterr().out
        # every seed file's rows (8 frames + W9 TAX-4's event_type values + W9 VIEW-1's crosswalk rows)
        n_seeds = len(walker.load_vocabulary_seeds())
        assert f"vocabulary seeds: register=0, update=0, unchanged={n_seeds}" in out

        async with pg.acquire() as conn:
            await conn.execute("UPDATE vocabulary_entries SET deprecated = now() "
                               "WHERE family='frame' AND value='thematic'")
            await walker.run(pg, reg, dry_run=False, families={"target"})
            assert await conn.fetchval("SELECT deprecated IS NOT NULL FROM vocabulary_entries "
                                       "WHERE family='frame' AND value='thematic'")
            await conn.execute("DELETE FROM vocabulary_entries WHERE family='frame'")
            assert (await frames.load_frame_catalog(conn)).status == frames.STATUS_EMPTY
            for rid, (members, membership) in off.items():
                assert await slice_._resolve_region_member_target_ids(conn, rid) == members
                assert await slice_.resolve_region_membership(conn, rid) == membership
    finally:
        await reg.stop()
        await pg.close()
