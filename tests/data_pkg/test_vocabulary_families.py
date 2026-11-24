# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W6 VOCAB-1 — namespaced vocabulary families.

`VocabularyEntry.family` admits `<profile>.<axis>` beside the platform's
closed, protected families; the `parent` chain is walked (bounded, cycle-
safe); and the cache degrades PER FAMILY (an unknown / malformed family is
`families_unloaded`, never a failed refresh).

The unit half is pure. The integration half runs on the real driver
(`migrated_pg`): rows are inserted into `vocabulary_entries` — which carries
no CHECK on `family`, so a stray family really can sit in the table — and the
cache and the routes are exercised over them.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from legba.data.postgres import PostgresStore
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps, build_router
from legba.data.registry.vocabulary_cache import VocabularyCache
from legba.data.schemas.vocabulary import (
    FAMILY_MALFORMED,
    FAMILY_PROTECTED,
    FAMILY_UNKNOWN,
    MAX_PARENT_DEPTH,
    PLATFORM_FAMILIES,
    FamilyError,
    VocabularyEntry,
    qualify_family,
    validate_family,
    walk_parents,
)

_NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def _entry(family: str, value: str, parent: str | None = None, **kw) -> VocabularyEntry:
    return VocabularyEntry(
        family=family, value=value, schema_uri="legba/vocabulary/1.0.0",
        introduced=_NOW, parent=parent, **kw,
    )


# ---------------------------------------------------------------------------
# Family admission
# ---------------------------------------------------------------------------


def test_platform_families_are_a_closed_set_and_admitted():
    assert PLATFORM_FAMILIES == {
        "entity_class", "relationship_type", "analyst_kind", "source_kind",
        "output_kind", "filter_kind", "enrichment_kind", "discovery_kind",
        "edge_family",
        # W7 TAXON-1
        "salience_class", "collection_gap_kind", "band", "source_layer",
        # W7 FRAME-1
        "frame",
        # W9 TAX-4
        "event_type",
        # W9 VIEW-1 (DEC-12)
        "crosswalk",
    }
    for fam in PLATFORM_FAMILIES:
        assert validate_family(fam) == fam


# ---------------------------------------------------------------------------
# The crosswalk family (W9 VIEW-1, DEC-12)
# ---------------------------------------------------------------------------


def test_a_crosswalk_entry_maps_a_profile_value_to_a_platform_value():
    e = _entry("crosswalk", "sanctions.programme:SDGT", parent="entity_class:organization")
    assert e.value == "sanctions.programme:SDGT"  # the profile's own value shape kept
    assert e.parent == "entity_class:organization"
    # a profile's own copy of an overridable family is a profile side too
    assert _entry("crosswalk", "geopolitics.band:elevated", parent="band:elevated").parent == "band:elevated"


@pytest.mark.parametrize("value, parent", [
    ("sanctions.programme:SDGT", None),               # no counterpart: not a row
    ("sanctions.programme:SDGT", "sanctions.list:x"),  # the counterpart must be PLATFORM
    ("sanctions.programme:SDGT", "crosswalk:x"),       # never a crosswalk of a crosswalk
    ("sanctions.programme:SDGT", "SDGT"),              # a bare parent is not a platform value
    ("entity_class:person", "entity_class:person"),    # the profile side must be namespaced
    ("sanctions.programme", "entity_class:person"),    # no value
    ("sanctions.programme: SDGT", "entity_class:person"),
    ("sanctions.entity_class:x", "entity_class:x"),    # a shadowing profile family
])
def test_a_malformed_crosswalk_entry_is_refused(value, parent):
    with pytest.raises(ValidationError):
        _entry("crosswalk", value, parent=parent)


@pytest.mark.parametrize("family", [
    "sanctions.programme", "sanctions.list", "geo.region.tier", "p1.a_b",
])
def test_namespaced_family_is_accepted(family):
    assert validate_family(family) == family
    e = _entry(family, "SDGT")
    assert e.family == family and e.value == "SDGT"  # a profile's values are its own


def test_bare_new_family_is_refused_family_unknown_with_a_namespace_hint():
    with pytest.raises(FamilyError) as ei:
        validate_family("programme")
    assert ei.value.code == FAMILY_UNKNOWN
    assert "<profile>.programme" in ei.value.hint
    with pytest.raises(ValidationError):
        _entry("programme", "x")


@pytest.mark.parametrize("family", [
    "Sanctions.programme", "a.b.c.d", "sanctions.", ".programme", "a..b",
    "1abc.x", "Bare", "sanctions.pro gramme",
])
def test_malformed_family_is_refused(family):
    with pytest.raises(FamilyError) as ei:
        validate_family(family)
    assert ei.value.code == FAMILY_MALFORMED


@pytest.mark.parametrize("family", [
    "entity_class.sub", "sanctions.entity_class", "sanctions.relationship_type.x",
])
def test_a_platform_name_inside_a_namespaced_family_is_protected(family):
    with pytest.raises(FamilyError) as ei:
        validate_family(family)
    assert ei.value.code == FAMILY_PROTECTED


def test_a_profile_cannot_redefine_a_platform_family():
    with pytest.raises(FamilyError) as ei:
        validate_family("entity_class", profile="sanctions")
    assert ei.value.code == FAMILY_PROTECTED
    with pytest.raises(FamilyError) as ei:
        qualify_family("sanctions", "entity_class")
    assert ei.value.code == FAMILY_PROTECTED


def test_a_profile_registers_only_under_its_own_namespace():
    assert qualify_family("sanctions", "programme") == "sanctions.programme"
    assert qualify_family("sanctions", "sanctions.list") == "sanctions.list"
    with pytest.raises(FamilyError) as ei:
        qualify_family("sanctions", "other.list")
    assert ei.value.code == FAMILY_PROTECTED
    assert validate_family("sanctions.list", profile="sanctions") == "sanctions.list"


def test_platform_value_shape_rules_are_unchanged():
    with pytest.raises(ValidationError):
        _entry("relationship_type", "snake_case")
    with pytest.raises(ValidationError):
        _entry("entity_class", "Upper")
    with pytest.raises(ValidationError):
        _entry("sanctions.programme", " padded ")


# ---------------------------------------------------------------------------
# Parent walk
# ---------------------------------------------------------------------------


def _idx(*entries: VocabularyEntry):
    return {(e.family, e.value): e for e in entries}


def test_parent_chain_walks_nearest_first_across_families():
    idx = _idx(
        _entry("sanctions.programme", "SDGT", parent="sanctions.programme:TERROR"),
        _entry("sanctions.programme", "TERROR", parent="sanctions.list:SDN"),
        _entry("sanctions.list", "SDN"),
    )
    assert walk_parents(idx, "sanctions.programme", "SDGT") == [
        ("sanctions.programme", "TERROR"), ("sanctions.list", "SDN"),
    ]
    assert walk_parents(idx, "sanctions.list", "SDN") == []
    assert walk_parents(idx, "sanctions.programme", "NOPE") == []


def test_a_bare_parent_is_a_value_of_the_entrys_own_family():
    idx = _idx(_entry("entity_class", "corporation", parent="organization"),
               _entry("entity_class", "organization"))
    assert walk_parents(idx, "entity_class", "corporation") == [("entity_class", "organization")]


def test_parent_walk_is_cycle_safe():
    idx = _idx(
        _entry("p.axis", "a", parent="b"),
        _entry("p.axis", "b", parent="c"),
        _entry("p.axis", "c", parent="a"),
    )
    assert walk_parents(idx, "p.axis", "a") == [("p.axis", "b"), ("p.axis", "c")]
    self_loop = _idx(_entry("p.axis", "x", parent="x"))
    assert walk_parents(self_loop, "p.axis", "x") == []


def test_parent_walk_is_bounded_at_depth_8():
    assert MAX_PARENT_DEPTH == 8
    n = 20
    idx = _idx(*[_entry("p.axis", f"n{i}", parent=f"n{i + 1}") for i in range(n)],
               _entry("p.axis", f"n{n}"))
    assert len(walk_parents(idx, "p.axis", "n0")) == 8


def test_a_missing_parent_ends_the_chain_without_raising():
    idx = _idx(_entry("p.axis", "a", parent="ghost"))
    assert walk_parents(idx, "p.axis", "a") == []


def test_parent_shape_is_checked():
    with pytest.raises(ValidationError):
        _entry("p.axis", "a", parent="x:y:z")
    with pytest.raises(ValidationError):
        _entry("p.axis", "a", parent="programme:SDGT")  # bare family, not admitted
    assert _entry("p.axis", "a", parent="entity_class:person").parent == "entity_class:person"


# ---------------------------------------------------------------------------
# Real driver — the cache and the routes
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg(migrated_pg):
    store = PostgresStore(migrated_pg)
    await store.connect()
    created: list[str] = []
    store.created = created  # type: ignore[attr-defined]
    yield store
    async with store.acquire() as conn:
        await conn.execute(
            "DELETE FROM vocabulary_entries WHERE family LIKE 'vt%' OR family LIKE 'Vt%'"
        )
    await store.close()


async def _insert(pg, family: str, value: str, parent: str | None = None) -> None:
    async with pg.acquire() as conn:
        await conn.execute(
            "INSERT INTO vocabulary_entries (family, value, parent) VALUES ($1, $2, $3)",
            family, value, parent,
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cache_loads_namespaced_families_and_walks_parents(pg):
    prof = f"vt{uuid4().hex[:6]}"
    await _insert(pg, f"{prof}.list", "SDN")
    await _insert(pg, f"{prof}.programme", "TERROR", f"{prof}.list:SDN")
    await _insert(pg, f"{prof}.programme", "SDGT", "TERROR")
    cache = VocabularyCache(pg)
    await cache.refresh()
    assert cache.contains(f"{prof}.programme", "SDGT")
    assert cache.contains("entity_class", "person")
    assert cache.make_validator(f"{prof}.programme")(["SDGT"]) == ["SDGT"]
    assert cache.ancestors(f"{prof}.programme", "SDGT") == [
        (f"{prof}.programme", "TERROR"), (f"{prof}.list", "SDN"),
    ]
    assert cache.descends_from(f"{prof}.programme", "SDGT", f"{prof}.list", "SDN")
    assert not cache.descends_from(f"{prof}.list", "SDN", f"{prof}.programme", "SDGT")
    assert [u for u in cache.families_unloaded if u["family"].startswith(prof)] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cache_survives_an_unknown_and_a_malformed_family(pg, caplog):
    prof = f"vt{uuid4().hex[:6]}"
    bare = f"vt{uuid4().hex[:6]}"
    bad = f"Vt{uuid4().hex[:6]}.Axis"
    shadow = f"{prof}.entity_class"
    await _insert(pg, f"{prof}.programme", "OK1")
    await _insert(pg, bare, "x")
    await _insert(pg, bare, "y")
    await _insert(pg, bad, "x")
    await _insert(pg, shadow, "x")
    cache = VocabularyCache(pg)
    with caplog.at_level("WARNING"):
        count = await cache.refresh()  # must not raise
    assert count > 0
    # the healthy families are loaded, the bad ones are not
    assert cache.contains("entity_class", "person")
    assert cache.contains(f"{prof}.programme", "OK1")
    assert not cache.contains(bare, "x")
    unloaded = {u["family"]: u for u in cache.families_unloaded}
    assert unloaded[bare]["code"] == FAMILY_UNKNOWN and unloaded[bare]["rows"] == 2
    assert unloaded[bad]["code"] == FAMILY_MALFORMED
    assert unloaded[shadow]["code"] == FAMILY_PROTECTED
    # one log line per unloaded family
    for fam in (bare, bad, shadow):
        assert sum(1 for r in caplog.records if "family unloaded" in r.getMessage()
                   and fam in r.getMessage()) == 1
    # a repaired table loads clean on the next refresh
    async with pg.acquire() as conn:
        await conn.execute("DELETE FROM vocabulary_entries WHERE family = ANY($1)",
                           [bare, bad, shadow])
    await cache.refresh()
    assert not [u for u in cache.families_unloaded if u["family"] in (bare, bad, shadow)]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_bad_row_in_a_loadable_family_is_skipped_not_fatal(pg):
    prof = f"vt{uuid4().hex[:6]}"
    await _insert(pg, "entity_class", f"Bad Value {prof}")  # violates lowercase rule
    await _insert(pg, f"{prof}.programme", "OK")
    cache = VocabularyCache(pg)
    try:
        await cache.refresh()
        assert cache.contains("entity_class", "person")
        assert cache.contains(f"{prof}.programme", "OK")
        assert any(r["value"] == f"Bad Value {prof}" for r in cache.rows_skipped)
        assert not any(u["family"] == "entity_class" for u in cache.families_unloaded)
    finally:
        async with pg.acquire() as conn:
            await conn.execute("DELETE FROM vocabulary_entries WHERE value = $1",
                               f"Bad Value {prof}")


class _PgOnly:
    def __init__(self, pg):
        self.pg = pg


@pytest_asyncio.fixture
async def client(pg):
    os.environ.pop(API_TOKEN_ENV, None)
    deps = RegistryAPIDeps(
        descriptor_registry=_PgOnly(pg),  # type: ignore[arg-type]
        stack_registry=None, vault=None, dlq=None, audit_logger=None,  # type: ignore[arg-type]
        vocabulary_cache=VocabularyCache(pg),
        nats_store=None, conversion_registry=None,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_router(deps), prefix="/api/v1/registry")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.mark.integration
@pytest.mark.asyncio
async def test_route_registers_a_namespaced_family_and_lists_it(client):
    fam = f"vt{uuid4().hex[:6]}.programme"
    r = await client.post(f"/api/v1/registry/vocabulary/{fam}", json={"value": "SDGT"})
    assert r.status_code == 201, r.text
    r = await client.post(
        f"/api/v1/registry/vocabulary/{fam}",
        json={"value": "IRAN", "parent": "SDGT"},
    )
    assert r.status_code == 201, r.text
    r = await client.get(f"/api/v1/registry/vocabulary/{fam}")
    assert sorted(v["value"] for v in r.json()) == ["IRAN", "SDGT"]
    r = await client.get("/api/v1/registry/vocabulary")
    body = r.json()
    assert r.status_code == 200
    assert {"family": fam, "values": 2} in body["families"]
    assert body["families_unloaded"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_route_refuses_bare_protected_and_malformed_families(client):
    r = await client.post("/api/v1/registry/vocabulary/programme", json={"value": "x"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == FAMILY_UNKNOWN
    assert "<profile>.programme" in r.json()["detail"]["hint"]

    r = await client.post(
        "/api/v1/registry/vocabulary/entity_class?profile=sanctions", json={"value": "x"},
    )
    assert r.status_code == 422 and r.json()["detail"]["code"] == FAMILY_PROTECTED

    r = await client.post("/api/v1/registry/vocabulary/sanctions.entity_class", json={"value": "x"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == FAMILY_PROTECTED

    r = await client.post("/api/v1/registry/vocabulary/Sanctions.Programme", json={"value": "x"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == FAMILY_MALFORMED

    # an operator extending a platform family (no profile) still works
    r = await client.post(
        "/api/v1/registry/vocabulary/entity_class", json={"value": f"vt_{uuid4().hex[:6]}"},
    )
    assert r.status_code == 201
    async with client._transport.app.state.registry_deps.descriptor_registry.pg.acquire() as conn:
        await conn.execute("DELETE FROM vocabulary_entries WHERE value LIKE 'vt\\_%'")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_index_route_shows_families_unloaded(client, pg):
    bare = f"vt{uuid4().hex[:6]}"
    await _insert(pg, bare, "x")
    deps = client._transport.app.state.registry_deps
    await deps.vocabulary_cache.refresh()
    r = await client.get("/api/v1/registry/vocabulary")
    assert r.status_code == 200
    body = r.json()
    hit = [u for u in body["families_unloaded"] if u["family"] == bare]
    assert hit and hit[0]["code"] == FAMILY_UNKNOWN and hit[0]["rows"] == 1
    assert any(f["family"] == "entity_class" for f in body["families"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_put_parent_is_shape_checked(client):
    fam = f"vt{uuid4().hex[:6]}.axis"
    r = await client.post(f"/api/v1/registry/vocabulary/{fam}", json={"value": "a"})
    eid = r.json()["id"]
    r = await client.put(f"/api/v1/registry/vocabulary/{fam}/{eid}", json={"parent": "x:y:z"})
    assert r.status_code == 422
    r = await client.put(f"/api/v1/registry/vocabulary/{fam}/{eid}", json={"parent": "b"})
    assert r.status_code == 200 and r.json()["parent"] == "b"
