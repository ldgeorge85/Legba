# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W6 SPLIT-1 — the one descriptor walker, end to end on the real driver.

``scripts/bringup_register_descriptors.py`` is what deploy.sh now runs for
every descriptor head. This file proves, against a FRESH migrated Postgres
(its own throwaway database — the walker registers ~280 active heads, which
must not leak into the shared session DB the rest of ``tests/data_pkg`` reads):

  1. a first apply registers every planned descriptor with no failure, at its
     declared lifecycle state, plus the vocabulary + credibility seeds;
  2. a ``--dry-run`` after it reports EVERY head ``unchanged`` and writes
     nothing (row counts in every walked table identical before and after);
  3. a second apply is all ``unchanged`` too — the walker is idempotent, and
     ``unchanged`` means "never written" (no new version rows).

Plus the pure pieces the live dry-run depends on: a stored head registered
under an older schema shape (missing a since-added field), or carrying the
circuit-breaker's annotation keys with its state only in the column, still
compares ``unchanged``.
"""
from __future__ import annotations

from tests._lifecycle_rows import lifecycle_text
import os
import pathlib
import sys
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.migrate import apply_primary_migrations
from legba.data.postgres import PostgresStore
from legba.data.registry.descriptor import DescriptorRegistry, Family
from legba.data.taxon_families import TAXON_FALLBACKS

_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts"
_HAD_PG_DB = "LEGBA_DATA_PG_DB" in os.environ
sys.path.insert(0, str(_SCRIPTS_DIR))
try:
    import bringup_register_descriptors as walker  # noqa: E402
finally:
    if not _HAD_PG_DB:
        os.environ.pop("LEGBA_DATA_PG_DB", None)

_ADMIN_DSN = "postgresql://legba:legba@127.0.0.1:5432/postgres"
_QUIET = {"unchanged", "retired", "parked"}


@pytest_asyncio.fixture
async def fresh_db():
    """A dedicated, migrated database, dropped afterwards."""
    name = f"legba_bringup_{uuid4().hex[:10]}"
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


async def _row_counts(pg: PostgresStore) -> dict[str, int]:
    out: dict[str, int] = {}
    async with pg.acquire() as conn:
        for fam in walker.WALKED_FAMILIES:
            out[fam] = await conn.fetchval(f"SELECT count(*) FROM {Family(fam).table}")
        out["vocabulary_entries"] = await conn.fetchval("SELECT count(*) FROM vocabulary_entries")
        out["source_credibility"] = await conn.fetchval("SELECT count(*) FROM source_credibility")
    return out


async def test_walker_registers_everything_then_is_idempotent(fresh_db, capsys):
    pg = PostgresStore(fresh_db)
    await pg.connect()
    reg = DescriptorRegistry(pg)
    await reg.start()
    try:
        planned, excluded = walker.plan()

        # 1. first apply — every planned descriptor registers, nothing fails.
        code, first = await walker.run(pg, reg, dry_run=False)
        failed = [(d.family, d.descriptor_id, d.detail) for d in first if d.action == "failed"]
        assert not failed, failed
        assert code == 0
        registered = [d for d in first if d.action == "register"]
        assert len(registered) == len(planned)

        # ...each head landed at the state its descriptor declares (read back).
        want = {(p.family, p.descriptor_id): p.body["identity"]["state"] for p in planned}
        async with pg.acquire() as conn:
            got = {}
            for fam in walker.WALKED_FAMILIES:
                for r in await conn.fetch(
                    f"SELECT descriptor_id, state FROM {Family(fam).table} WHERE is_head"
                ):
                    got[(fam, r["descriptor_id"])] = r["state"]
            kinds = {r["value"] for r in await conn.fetch(
                "SELECT value FROM vocabulary_entries WHERE family='analyst_kind'")}
            taxa: dict[str, set[str]] = {}
            for r in await conn.fetch(
                "SELECT family, value FROM vocabulary_entries "
                "WHERE family = ANY($1::text[]) AND deprecated IS NULL",
                list(TAXON_FALLBACKS),
            ):
                taxa.setdefault(r["family"], set()).add(r["value"])
            seeded = {(r["family"], r["value"]) for r in await conn.fetch(
                "SELECT family, value FROM vocabulary_entries WHERE family <> 'analyst_kind'")}
        # W7 FRAME-1 — every vocabulary seed file's entries landed
        assert {(s.family, s.value) for s in walker.load_vocabulary_seeds()} <= seeded
        wrong = {k: (v, got.get(k)) for k, v in want.items() if got.get(k) != v}
        assert not wrong, wrong
        assert set(walker.NON_BUILTIN_ANALYST_KINDS) <= kinds
        # W7 TAXON-1: the four taxon families are seeded, equal to their literals
        assert taxa == {f: set(v) for f, v in TAXON_FALLBACKS.items()}
        # nothing excluded by RETIRED.md was registered
        assert not {(e.family, e.descriptor_id) for e in excluded} & set(got)

        # 2. dry run — all unchanged, and NOTHING written.
        before = await _row_counts(pg)
        capsys.readouterr()
        code, dry = await walker.run(pg, reg, dry_run=True)
        printed = capsys.readouterr().out
        assert code == 0
        noisy = [(d.action, d.family, d.descriptor_id, d.detail) for d in dry if d.action not in _QUIET]
        assert not noisy, noisy
        assert "DRY RUN — nothing written" in printed
        assert "would-update" not in printed and "would-retire" not in printed
        assert "would add taxon values" not in printed
        assert await _row_counts(pg) == before

        # 3. second apply — idempotent: all unchanged, no new version rows.
        code, second = await walker.run(pg, reg, dry_run=False)
        assert code == 0
        noisy = [(d.action, d.family, d.descriptor_id, d.detail) for d in second if d.action not in _QUIET]
        assert not noisy, noisy
        assert await _row_counts(pg) == before
    finally:
        await reg.stop()
        await pg.close()


def _one(family: str, descriptor_id: str):
    planned, _ = walker.plan()
    walker.register_kinds_in_process()
    return next(p for p in planned if p.family == family and p.descriptor_id == descriptor_id)


def test_the_walker_plans_the_profile_projector_as_a_draft():
    """W9 VIEW-1 — the projector's FILE ships ``draft`` (files never schedule);
    its live ``active`` state is a LIFECYCLE.md row since the first live build
    (roll 39), so the walker plans it unchanged; it validates as a deterministic
    analyst with no token budget, and the crosswalk seed file is a vocabulary
    seed, never a planned descriptor."""
    import yaml as _yaml
    p = _one("analyst", "profile_projector")
    model = walker.validate(p)
    # the PLAN carries the LIFECYCLE overlay (active since roll 39); the FILE ships draft
    assert model.identity.state.value == "active"
    shipped = _yaml.safe_load(
        (walker.DESCRIPTORS_DIR / "analyst_profile_projector.yaml").read_text(encoding="utf-8"))
    assert shipped["identity"]["state"] == "draft"
    assert p.body["method"]["sub_handler"] == "profile_projector"
    assert p.body["method"]["budget_tokens_per_day"] == 0
    lifecycle = lifecycle_text(walker.DESCRIPTORS_DIR)
    assert "| analyst | profile_projector | active | draft |" in lifecycle
    planned, _ = walker.plan()
    assert "crosswalk" not in {pp.descriptor_id for pp in planned}
    seeds = [s for s in walker.load_vocabulary_seeds() if s.family == "crosswalk"]
    assert seeds and all(s.parent for s in seeds)


def test_an_older_schema_shape_still_compares_unchanged():
    """A head stored before a field existed (here: the analyst ``method`` without
    the later ``options`` / ``bounded_question`` keys) is the same content."""
    p = _one("analyst", "entity_gc")
    model = walker.validate(p)
    stored = model.model_dump(mode="json", by_alias=True)
    stored["method"].pop("options", None)
    stored["method"].pop("bounded_question", None)
    stored["identity"]["version"] = "f" * 64
    heads = {("analyst", "entity_gc"): ("f" * 64, model.identity.state.value, stored)}
    (d,) = walker.decide([p], [], heads)
    assert d.action == "unchanged", d


def test_the_state_column_wins_and_annotations_are_ignored():
    """The source circuit-breaker pauses by column and leaves the body saying
    ``active`` plus two keys the schema does not declare."""
    p = _one("source", "source.aib.news")
    model = walker.validate(p)
    assert model.identity.state.value == "paused"
    stored = model.model_dump(mode="json", by_alias=True)
    stored["identity"]["state"] = "active"
    stored["auto_paused_at"] = "2026-10-02T00:18:30+00:00"
    stored["auto_paused_reason"] = "Exceeded 20 consecutive failed polls"
    heads = {("source", p.descriptor_id): ("a" * 64, "paused", stored)}
    (d,) = walker.decide([p], [], heads)
    assert d.action == "unchanged", d


def test_a_real_difference_is_an_update_naming_the_keys():
    p = _one("analyst", "signal_embedder")
    model = walker.validate(p)
    stored = model.model_dump(mode="json", by_alias=True)
    stored["method"]["options"]["max_embeds"] = 999
    heads = {("analyst", "signal_embedder"): ("b" * 64, "active", stored)}
    (d,) = walker.decide([p], [], heads)
    assert d.action == "update" and d.detail == "method.options", d


def test_retired_rows_and_parked_rows():
    _, excluded = walker.plan()
    retired = next(e for e in excluded if e.status == "retired" and e.family == "analyst")
    parked = next(e for e in excluded if e.status == "parked")
    heads = {(retired.family, retired.descriptor_id): ("c" * 64, "active", {})}
    out = {d.descriptor_id: d for d in walker.decide([], [retired, parked], heads)}
    assert out[retired.descriptor_id].action == "retire"      # would-retire on a dry run
    assert out[parked.descriptor_id].action == "parked"
    heads[(retired.family, retired.descriptor_id)] = ("c" * 64, "retired", {})
    (d,) = walker.decide([], [retired], heads)
    assert d.action == "retired"
    heads = {(parked.family, parked.descriptor_id): ("d" * 64, "active", {})}
    (d,) = walker.decide([], [parked], heads)
    assert d.action == "failed"                                # a parked row may not hide a live head


def test_a_live_retired_head_the_tree_still_plans_fails_loudly():
    p = _one("analyst", "entity_gc")
    heads = {("analyst", "entity_gc"): ("e" * 64, "retired", {})}
    (d,) = walker.decide([p], [], heads)
    assert d.action == "failed" and "RETIRED.md" in d.detail


@pytest.mark.parametrize(
    "bad", ["dup", "unlisted_retired", "orphan_row", "lifecycle_stray", "lifecycle_repeats_ship"],
)
def test_plan_errors(tmp_path, bad):
    """The plan refuses a tree it cannot walk as declared — before any DB I/O."""
    d = tmp_path / "descriptors"
    d.mkdir()
    src = pathlib.Path(walker.DESCRIPTORS_DIR)
    (d / "analyst_entity_gc.yaml").write_text((src / "analyst_entity_gc.yaml").read_text())
    md = "| status | family | id | registrar | why |\n|---|---|---|---|---|\n"
    if bad == "dup":
        (d / "analyst_entity_gc_copy.yaml").write_text((src / "analyst_entity_gc.yaml").read_text())
    elif bad == "unlisted_retired":
        (d / "analyst_country_assessor.yaml").write_text((src / "analyst_country_assessor.yaml").read_text())
    elif bad == "orphan_row":
        md += "| retired | analyst | no_such_analyst | x | y |\n"
    lc = "| family | id | state | file ships |\n|---|---|---|---|\n"
    if bad == "lifecycle_stray":
        lc += "| analyst | no_such_analyst | active | draft |\n"
    elif bad == "lifecycle_repeats_ship":
        lc += "| analyst | entity_gc | active | active |\n"   # the file already ships active
    (d / "RETIRED.md").write_text(md)
    (d / "LIFECYCLE.md").write_text(lc)
    with pytest.raises(walker.PlanError):
        walker.plan(d, include_catalog=bad in ("orphan_row", "lifecycle_stray"))
