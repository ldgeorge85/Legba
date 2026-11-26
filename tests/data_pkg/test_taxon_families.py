# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Four code-literal vocabularies become registered families.

``salience_class`` / ``collection_gap_kind`` / ``band`` / ``source_layer``
are platform vocabulary families seeded by the bring-up walker from the
literals in :mod:`legba.data.taxon_families`. This file pins:

  1. THE DRIFT GUARD — the literal is the owning modules' constant (and the
     migration CHECKs'), and the walker's seeded rows, read back on the real
     driver, equal it;
  2. THE FALLBACK — a family the installed vocabulary does not hold (absent,
     empty, unloaded, or a source that raises) reads the literal; a loaded
     family reads through; a CHECK-bound family can be narrowed, never widened;
  3. THE WRITE-TIME BINDING — a value outside the family binds
     ``unclassified`` (``bound`` / ``unclassified``, TAG-1's words); with no
     vocabulary installed every writer is byte-identical to before;
  4. ENUMERABLE — the four families appear on ``GET /registry/vocabulary``
     through the router and the deps bundle, and the registry app installs
     its cache as the read-through source;
  5. PROFILES — ``<profile>.<family>`` is admitted for the four; the bare
     family and every other placement stay protected.
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data import taxon_families as taxa
from legba.data.analysts import signal_salience
from legba.data.analysts.deterministic_handlers import (
    _reference_gap_dispatch,
    collection_gap,
    scorecard_banding,
)
from legba.data.config import PostgresConfig
from legba.data.layers import _vocab as layer_vocab
from legba.data.migrate import apply_primary_migrations
from legba.data.postgres import PostgresStore
from legba.data.registry import since_api
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps, build_router
from legba.data.registry.vocabulary_cache import VocabularyCache
from legba.data.schemas.vocabulary import (
    FAMILY_PROTECTED,
    PLATFORM_FAMILIES,
    PROFILE_OVERRIDABLE_FAMILIES,
    FamilyError,
    VocabularyEntry,
    qualify_family,
    validate_family,
)

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO / "scripts"
_MIGRATIONS = _REPO / "src" / "legba" / "data" / "migrations"
_HAD_PG_DB = "LEGBA_DATA_PG_DB" in os.environ
sys.path.insert(0, str(_SCRIPTS_DIR))
try:
    import bringup_register_descriptors as walker  # noqa: E402
finally:
    if not _HAD_PG_DB:
        os.environ.pop("LEGBA_DATA_PG_DB", None)

_ADMIN_DSN = "postgresql://legba:legba@127.0.0.1:5432/postgres"
_NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_installed_vocabulary():
    """Every test starts and ends with nothing installed (a registry app
    built elsewhere in the session installs its cache)."""
    prior = taxa.installed_vocabulary()
    taxa.install_vocabulary(None)
    yield
    taxa.install_vocabulary(prior)


class _Vocab:
    """A duck-typed vocabulary source (what a VocabularyCache exposes)."""

    def __init__(self, values: dict[str, set[str]], unloaded: tuple[str, ...] = ()):
        self._values = values
        self.families_unloaded = [{"family": f, "code": "family_unknown"} for f in unloaded]

    def values(self, family: str) -> set[str]:
        return set(self._values.get(family, set()))


class _Raising:
    families_unloaded: list = []

    def values(self, family: str) -> set[str]:
        raise RuntimeError("cache gone")


# ---------------------------------------------------------------------------
# 1. The drift guard — the literal IS the owning modules' constant
# ---------------------------------------------------------------------------


def test_the_literals_are_the_owning_modules_constants():
    assert signal_salience.EVENT_CLASSES == frozenset(taxa.SALIENCE_CLASSES)
    assert scorecard_banding.BAND_LADDER == taxa.BAND_LADDER
    assert scorecard_banding.INSUFFICIENT == taxa.INSUFFICIENT_BAND
    # the registry's independent re-spelling of the ladder (since_api) agrees
    assert since_api.BAND_LADDER == taxa.BAND_LADDER
    assert since_api.INSUFFICIENT_BAND == taxa.INSUFFICIENT_BAND
    assert set(scorecard_banding.SEVERITY_TO_BAND.values()) <= set(taxa.BAND_LADDER)
    assert layer_vocab.LAYER_VOCAB == taxa.SOURCE_LAYERS
    # every origin a writer stamps is a collection_gap_kind
    gap = collection_gap.build_gap_requirement_rows([{
        "desk": "d", "dimension": "escalation", "latest_scorecard_id": str(uuid4()),
    }])
    req = collection_gap.build_source_request_row({"id": uuid4()}, priority_rank=0)
    assert {gap[0]["origin"], req["origin"], _reference_gap_dispatch.ORIGIN} == set(
        taxa.COLLECTION_GAP_KINDS
    )


def _check_list(sql: str, constraint: str) -> set[str]:
    m = re.search(
        rf"ADD CONSTRAINT {constraint}\s+CHECK \(\w+ IN \(([^)]*)\)\)", sql, re.S,
    )
    assert m, constraint
    return set(re.findall(r"'([^']+)'", m.group(1)))


def test_the_check_bound_families_mirror_their_migration_checks():
    sql = (_MIGRATIONS / "0193_collection_requirements_reference_gap.sql").read_text()
    assert _check_list(sql, "collection_requirements_origin_check") == set(
        taxa.COLLECTION_GAP_KINDS
    )
    sql = (_MIGRATIONS / "0214_source_layers.sql").read_text()
    m = re.search(r"CONSTRAINT source_layers_layer_vocab\s+CHECK \(layer IN \(([^)]*)\)\)", sql, re.S)
    assert m and set(re.findall(r"'([^']+)'", m.group(1))) == set(taxa.SOURCE_LAYERS)
    assert set(taxa.CHECK_BOUND_FAMILIES) == {taxa.COLLECTION_GAP_KIND, taxa.SOURCE_LAYER}


def test_the_four_are_platform_families_and_every_seed_row_is_a_valid_entry():
    assert set(taxa.TAXON_FAMILIES) == {
        "salience_class", "collection_gap_kind", "band", "source_layer",
    }
    assert set(taxa.TAXON_FAMILIES) <= PLATFORM_FAMILIES
    rows = taxa.taxon_seed_rows()
    assert len(rows) == 12 + 3 + 6 + 6
    assert len({(f, v) for f, v, _ in rows}) == len(rows)
    for fam, val, note in rows:
        assert note and note == note.strip()
        e = VocabularyEntry(
            family=fam, value=val, schema_uri="legba/vocabulary/1.0.0",
            introduced=_NOW, notes=note,
        )
        assert (e.family, e.value) == (fam, val)


# ---------------------------------------------------------------------------
# 2. The fallback, per family
# ---------------------------------------------------------------------------


def test_nothing_installed_reads_the_literal():
    for fam, lit in taxa.TAXON_FALLBACKS.items():
        assert taxa.taxon_values(fam) == lit


@pytest.mark.parametrize("source", [
    _Vocab({}),                                                    # family absent
    _Vocab({"band": set()}),                                       # family empty
    _Vocab({"band": {"low"}}, unloaded=("band",)),                 # family unloaded
    _Raising(),                                                    # source broken
])
def test_an_unloaded_family_reads_the_literal(source):
    assert taxa.taxon_values(taxa.BAND, vocab=source) == taxa.BANDS
    taxa.install_vocabulary(source)
    assert taxa.taxon_values(taxa.BAND) == taxa.BANDS


def test_a_loaded_family_reads_through_canonical_order_first():
    v = _Vocab({"salience_class": {"other", "leader_death", "zz_new", "aa_new"}})
    assert taxa.taxon_values(taxa.SALIENCE_CLASS, vocab=v) == (
        "leader_death", "other", "aa_new", "zz_new",
    )
    # the degrade is PER FAMILY: band is absent from the same source
    assert taxa.taxon_values(taxa.BAND, vocab=v) == taxa.BANDS


def test_a_check_bound_family_narrows_but_never_widens():
    v = _Vocab({
        "source_layer": {"official", "physical", "rumour_mill"},
        "collection_gap_kind": {"brand_new_kind"},
    })
    assert taxa.taxon_values(taxa.SOURCE_LAYER, vocab=v) == ("official", "physical")
    # nothing of the CHECK set left -> the literal, never an empty family
    assert taxa.taxon_values(taxa.COLLECTION_GAP_KIND, vocab=v) == taxa.COLLECTION_GAP_KINDS


# ---------------------------------------------------------------------------
# 3. Write-time binding
# ---------------------------------------------------------------------------


def test_bind_taxon_bound_and_unclassified(caplog):
    b = taxa.bind_taxon(taxa.BAND, "watch")
    assert (b.value, b.raw, b.status, b.bound) == ("watch", "watch", "bound", True)
    key = f"moderate_{uuid4().hex[:6]}"
    with caplog.at_level(logging.WARNING):
        u = taxa.bind_taxon(taxa.BAND, key)
        taxa.bind_taxon(taxa.BAND, key)
    assert (u.value, u.raw, u.status, u.bound) == ("unclassified", key, "unclassified", False)
    # one log line per new unknown (family, value) per process
    assert sum(1 for r in caplog.records if key in r.getMessage()) == 1
    assert taxa.bind_taxon(taxa.SOURCE_LAYER, None).status == "unclassified"


def test_salience_writer_is_byte_identical_without_a_vocabulary():
    assert signal_salience._coerce_event_class("Kinetic Strike") == "kinetic_strike"
    assert signal_salience._coerce_event_class("protest") == "coup_unrest"
    assert signal_salience._coerce_event_class("gibberish") == "other"
    assert signal_salience._coerce_event_class(None) == "other"


def test_salience_writer_binds_a_value_the_family_does_not_hold():
    taxa.install_vocabulary(_Vocab({"salience_class": {"kinetic_strike", "other"}}))
    assert signal_salience._coerce_event_class("airstrike") == "kinetic_strike"
    # coerced to a class the (narrowed) family does not hold -> unclassified
    assert signal_salience._coerce_event_class("sports") == "unclassified"


def test_band_is_bound_when_the_verdict_is_persisted():
    assert scorecard_banding._insufficient("no-finding").to_dict()["band"] == (
        "insufficient-evidence"
    )
    taxa.install_vocabulary(_Vocab({"band": {"low", "watch"}}))
    assert scorecard_banding._insufficient("no-finding").to_dict()["band"] == "unclassified"


def test_layer_gate_reads_the_family():
    assert all(layer_vocab.is_valid_layer(x) for x in taxa.SOURCE_LAYERS)
    assert not layer_vocab.is_valid_layer("rumour_mill")
    taxa.install_vocabulary(_Vocab({"source_layer": {"official", "foreign_press"}}))
    assert layer_vocab.is_valid_layer("official")
    assert not layer_vocab.is_valid_layer("physical")


async def test_collection_gap_writer_refuses_an_unclassified_origin_by_name(caplog):
    row = collection_gap.build_source_request_row({"id": uuid4()}, priority_rank=0)
    row["origin"] = "invented_kind"
    stats: dict[str, int] = {}
    with caplog.at_level(logging.WARNING):
        # pool=None: the refusal happens before any I/O
        assert await collection_gap.write_requirements(None, [row], stats=stats) == 0
    assert stats == {"existing": 0, "failed": 1}
    assert any("origin_unclassified" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# 5. Profiles may carry their own copy
# ---------------------------------------------------------------------------


def test_a_profile_may_carry_its_own_taxon_family():
    assert PROFILE_OVERRIDABLE_FAMILIES == set(taxa.TAXON_FAMILIES)
    for fam in taxa.TAXON_FAMILIES:
        assert validate_family(f"geopolitics.{fam}") == f"geopolitics.{fam}"
        assert validate_family(f"geopolitics.{fam}", profile="geopolitics")
        assert qualify_family("geopolitics", fam) == f"geopolitics.{fam}"
        with pytest.raises(FamilyError) as ei:
            validate_family(fam, profile="geopolitics")
        assert ei.value.code == FAMILY_PROTECTED
    for bad in ("geopolitics.band.sub", "geopolitics.x.band", "geopolitics.entity_class"):
        with pytest.raises(FamilyError) as ei:
            validate_family(bad)
        assert ei.value.code == FAMILY_PROTECTED
    with pytest.raises(FamilyError):
        qualify_family("geopolitics", "entity_class")


# ---------------------------------------------------------------------------
# 1+4. Seeded on the real driver, enumerable through the route
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fresh_db():
    """A dedicated, migrated database, dropped afterwards (the seeds are
    platform rows; they must not leak into the shared session DB)."""
    name = f"legba_taxon_{uuid4().hex[:10]}"
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


async def _taxon_rows(pg) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    async with pg.acquire() as conn:
        for r in await conn.fetch(
            "SELECT family, value FROM vocabulary_entries "
            "WHERE family = ANY($1::text[]) AND deprecated IS NULL",
            list(taxa.TAXON_FAMILIES),
        ):
            out.setdefault(r["family"], set()).add(r["value"])
    return out


@pytest.mark.integration
async def test_walker_seeds_equal_the_literals_and_the_route_enumerates_them(fresh_db, capsys):
    pg = PostgresStore(fresh_db)
    await pg.connect()
    try:
        # dry run: reports all 26, writes nothing
        missing = await walker.seed_taxon_families(pg, dry_run=True)
        assert len(missing) == 27
        assert "would add taxon values" in capsys.readouterr().out
        assert await _taxon_rows(pg) == {}

        # apply: the rows read back equal the literals
        await walker.seed_taxon_families(pg, dry_run=False)
        assert await _taxon_rows(pg) == {f: set(v) for f, v in taxa.TAXON_FALLBACKS.items()}
        async with pg.acquire() as conn:
            note = await conn.fetchval(
                "SELECT notes FROM vocabulary_entries WHERE family='band' AND value='low'"
            )
        assert note == taxa.TAXON_NOTES[("band", "low")]

        # idempotent; an operator's deprecation is not undone by a re-run
        async with pg.acquire() as conn:
            await conn.execute(
                "UPDATE vocabulary_entries SET deprecated = now() "
                "WHERE family='source_layer' AND value='physical'"
            )
        assert await walker.seed_taxon_families(pg, dry_run=False) == []

        # the cache reads through: the deprecated layer narrows the family
        cache = VocabularyCache(pg)
        await cache.refresh()
        assert cache.families_unloaded == []
        assert taxa.taxon_values(taxa.BAND, vocab=cache) == taxa.BANDS
        assert taxa.taxon_values(taxa.SALIENCE_CLASS, vocab=cache) == taxa.SALIENCE_CLASSES
        assert taxa.taxon_values(taxa.SOURCE_LAYER, vocab=cache) == taxa.SOURCE_LAYERS[:-1]
        taxa.install_vocabulary(cache)
        assert not layer_vocab.is_valid_layer("physical")

        # enumerable: GET /registry/vocabulary through the router + deps bundle
        os.environ.pop(API_TOKEN_ENV, None)

        class _PgOnly:
            def __init__(self, pg):
                self.pg = pg

        deps = RegistryAPIDeps(
            descriptor_registry=_PgOnly(pg),  # type: ignore[arg-type]
            stack_registry=None, vault=None, dlq=None, audit_logger=None,  # type: ignore[arg-type]
            vocabulary_cache=cache, nats_store=None, conversion_registry=None,
        )
        app = FastAPI()
        app.state.registry_deps = deps
        app.include_router(build_router(deps), prefix="/api/v1/registry")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/v1/registry/vocabulary")
            assert r.status_code == 200, r.text
            fams = {f["family"]: f["values"] for f in r.json()["families"]}
            assert fams["salience_class"] == 12
            assert fams["collection_gap_kind"] == 3
            assert fams["band"] == 6
            assert fams["source_layer"] == 5  # one deprecated above
            r = await c.get("/api/v1/registry/vocabulary/band")
            assert r.status_code == 200
            assert {v["value"] for v in r.json()} == set(taxa.BANDS)
    finally:
        await pg.close()


def test_the_registry_app_installs_its_cache(migrated_pg, monkeypatch):
    from legba.data.config import NatsConfig
    from legba.data.registry.credentials import MASTER_KEY_ENV
    from legba.data.registry.server import create_app

    monkeypatch.setenv(MASTER_KEY_ENV, "00112233445566778899" * 3 + "0011")
    monkeypatch.setenv("LEGBA_REGISTRY_SIGNING_KEY", "22" * 32)
    app = create_app(
        pg_config=migrated_pg, nats_config=NatsConfig.from_env(),
        enable_healthcheck_loop=False, enable_vocabulary_subscription=False,
    )
    assert taxa.installed_vocabulary() is app.state.registry_deps.vocabulary_cache
