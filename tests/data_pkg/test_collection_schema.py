# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 7g-1 — the `collection` descriptor family: schema, taxonomy, registry.

Three halves, in order of how far they reach:

1. **The schema as a gate.** ``legba/collection/1.0.0`` accepts the shipped
   pilot manifest VERBATIM — the manifest was drafted and operator-approved
   before this schema existed, so the schema has to fit the file, not the
   other way round — and refuses, one property per test, the three things
   §2 says fail closed: a missing/unknown ``licence_class``, a ``firewall``
   that does not name all eight surfaces, and an ``origin_class`` that is
   not history.

2. **The file-convention taxonomy.** Every committed
   ``descriptors/collection_*.yaml`` parses, and no collection is named
   ``source_*`` — the prefix is load-bearing, because the source machinery
   globs ``descriptors/source_*.yaml`` and would pick a mis-prefixed
   collection up as a live feed. Pinned here the same way
   ``test_source_class_taxonomy.py`` pins the source side.

3. **The registry, through the real binding path.** Register → get → typed →
   transition (``draft`` → ``reviewed`` → ``loaded``) → retire
   (``superseded``) over the ASGI app, not by calling handlers. The
   collection lifecycle is its own four-state machine, so this is the test
   that would catch a family wired to the shared one.
"""

from __future__ import annotations

import os
import pathlib
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from nacl.signing import SigningKey
from pydantic import ValidationError

from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.provenance.access import ACCESS_CLASSES
from legba.data.provenance.origin import LIVE_CLASSES
from legba.data.registry.api import API_TOKEN_ENV, RegistryAPIDeps, build_router
from legba.data.registry.audit import AuditLogger
from legba.data.registry.credentials import MASTER_KEY_ENV, CredentialVault
from legba.data.registry.descriptor import Family
from legba.data.registry.descriptor import DescriptorRegistry
from legba.data.registry.descriptor_families import (
    state_machine_for,
    terminal_state_for,
)
from legba.data.registry.dlq import DescriptorDeadLetter
from legba.data.registry.signing import SigningIdentity
from legba.data.registry.stack import StackRegistry
from legba.data.registry.vocabulary_cache import VocabularyCache
from legba.data.schemas.collection import (
    COLLECTION_TRANSITIONS,
    DOCUMENT_LOADER_KINDS,
    FENCED_SURFACES,
    OPT_IN_READERS,
    ORIGIN_SHAPE_CLASS,
    CollectionDescriptor,
    CollectionState,
    LoaderBlock,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DESCRIPTORS_DIR = REPO_ROOT / "descriptors"
PILOT = DESCRIPTORS_DIR / "collection_series_pilot_2016_2026.yaml"
MINI = (
    pathlib.Path(__file__).parent
    / "fixtures" / "collection_manifest_pilot" / "mini_manifest.yaml"
)

_TEST_MASTER_KEY_HEX = "0011223344556677889900112233445566778899001122334455667788990011"
os.environ.setdefault(MASTER_KEY_ENV, _TEST_MASTER_KEY_HEX)
os.environ.setdefault("LEGBA_REGISTRY_SIGNING_KEY", "44" * 32)


def _body(path: pathlib.Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _parse(body: dict[str, Any]) -> CollectionDescriptor:
    """The REAL parse path: the registry validates the wire with strict=False."""
    return CollectionDescriptor.model_validate(body, strict=False)


# ---------------------------------------------------------------------------
# 1. The schema accepts the shipped manifest, verbatim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [PILOT, MINI], ids=["pilot", "fixture_mini"])
def test_committed_manifest_parses_verbatim(path: pathlib.Path) -> None:
    desc = _parse(_body(path))
    assert desc.identity.schema_uri == "legba/collection/1.0.0"
    assert desc.identity.state is CollectionState.DRAFT
    assert desc.identity.abstraction_level.value == "L0"
    assert desc.manifest.series, "a collection with no series is not a collection"


def test_pilot_manifest_is_the_holding_the_design_note_describes() -> None:
    desc = _parse(_body(PILOT))
    assert desc.origin_shape == "archive_only"
    assert desc.origin_class == "archive"
    assert desc.licence_class == "public"
    assert {s.subject for s in desc.subjects} == {"US", "IL", "IR", "UA"}
    assert desc.window.valid_from == "2016-01-01"
    assert desc.window.valid_to == "2025-12-31"
    assert len(desc.manifest.series) == 11
    pairs = sum(len(s.subjects) for s in desc.manifest.series)
    assert pairs == 44
    assert desc.loader.kind == "series_api"
    assert desc.loader.budget_tokens_per_day == 0
    # Both fetch shapes are present — the loader must branch on `fetch.mode`,
    # not on the provider's name.
    assert {s.fetch.mode for s in desc.manifest.series} == {
        "json_api", "bulk_jsonl",
    }


def test_manifest_hash_is_stable_and_is_not_the_descriptor_hash() -> None:
    """The collection VERSION is the hash of the MANIFEST (§2).

    Re-approving a licence line or closing an open item must not invalidate a
    load that already happened, so the version this test pins moves only when
    the holding itself does.
    """
    body = _body(PILOT)
    first = _parse(body).manifest_hash()
    assert first == _parse(_body(PILOT)).manifest_hash()
    touched = dict(body)
    touched["open_items"] = []
    assert _parse(touched).manifest_hash() == first
    widened = yaml.safe_load(PILOT.read_text(encoding="utf-8"))
    widened["manifest"]["series"][0]["subjects"] = ["US"]
    assert _parse(widened).manifest_hash() != first


# ---------------------------------------------------------------------------
# 1b. …and refuses, fail-closed, the three things §2 names
# ---------------------------------------------------------------------------


def test_missing_licence_class_refuses() -> None:
    body = _body(PILOT)
    body.pop("licence_class")
    with pytest.raises(ValidationError) as exc:
        _parse(body)
    assert "licence_class" in str(exc.value)


def test_unknown_licence_class_refuses_naming_the_vocabulary() -> None:
    body = _body(PILOT)
    body["licence_class"] = "probably_fine"
    with pytest.raises(ValidationError) as exc:
        _parse(body)
    message = str(exc.value)
    assert "access-class vocabulary" in message
    assert all(cls in message for cls in ACCESS_CLASSES)


def test_licence_block_without_terms_text_refuses() -> None:
    """A recorded licence is holder + name + URL + text + attribution.

    A provider block that names a licence but records none of its terms is a
    guess, and a guess must not be loadable.
    """
    body = _body(PILOT)
    body["licence"]["providers"]["world_bank"].pop("text")
    with pytest.raises(ValidationError):
        _parse(body)


def test_a_series_provider_with_no_licence_block_refuses() -> None:
    body = _body(PILOT)
    body["licence"]["providers"].pop("eia")
    with pytest.raises(ValidationError) as exc:
        _parse(body)
    assert "no `licence.providers` block" in str(exc.value)


@pytest.mark.parametrize("dropped", sorted(FENCED_SURFACES))
def test_firewall_missing_any_one_surface_refuses(dropped: str) -> None:
    body = _body(PILOT)
    body["firewall"]["excluded_from"] = [
        s for s in FENCED_SURFACES if s != dropped
    ]
    with pytest.raises(ValidationError) as exc:
        _parse(body)
    assert "EXACTLY the eight fenced surfaces" in str(exc.value)
    assert dropped in str(exc.value)


def test_firewall_with_an_unknown_surface_refuses() -> None:
    body = _body(PILOT)
    body["firewall"]["excluded_from"] = [*FENCED_SURFACES, "vibes"]
    with pytest.raises(ValidationError) as exc:
        _parse(body)
    assert "vibes" in str(exc.value)


def test_firewall_opt_in_reader_vocabulary_is_closed() -> None:
    body = _body(PILOT)
    body["firewall"]["readers_opt_in"] = [*OPT_IN_READERS, "alerts"]
    with pytest.raises(ValidationError) as exc:
        _parse(body)
    assert "unknown reader" in str(exc.value)


@pytest.mark.parametrize("live", sorted(LIVE_CLASSES))
def test_a_live_origin_class_refuses(live: str) -> None:
    body = _body(PILOT)
    body["origin_class"] = live
    with pytest.raises(ValidationError) as exc:
        _parse(body)
    assert "is a LIVE class" in str(exc.value)


def test_origin_shape_and_origin_class_must_agree() -> None:
    body = _body(PILOT)
    body["origin_class"] = "backfill_native"     # archive_only stamps 'archive'
    with pytest.raises(ValidationError) as exc:
        _parse(body)
    assert "stamps" in str(exc.value)


def test_every_origin_shape_maps_to_a_history_class() -> None:
    assert set(ORIGIN_SHAPE_CLASS) == {
        "source_with_history", "source_without_history", "archive_only",
    }
    assert not (set(ORIGIN_SHAPE_CLASS.values()) & set(LIVE_CLASSES))


def test_a_nonzero_token_budget_refuses() -> None:
    """House rule: every descriptor declares it, and a collection's is 0."""
    body = _body(PILOT)
    body["loader"]["budget_tokens_per_day"] = 1
    with pytest.raises(ValidationError) as exc:
        _parse(body)
    assert "must be 0" in str(exc.value)


def test_a_series_naming_an_undeclared_subject_refuses() -> None:
    body = _body(PILOT)
    body["manifest"]["series"][0]["subjects"] = ["US", "ZZ"]
    with pytest.raises(ValidationError) as exc:
        _parse(body)
    assert "ZZ" in str(exc.value)


def test_a_series_widening_past_the_collection_window_refuses() -> None:
    body = _body(PILOT)
    body["manifest"]["series"][0]["valid_from"] = "2004-01-01"
    with pytest.raises(ValidationError) as exc:
        _parse(body)
    assert "widens past" in str(exc.value)


def test_coverage_declaring_values_with_no_years_refuses() -> None:
    """Absence is absence, and its inverse is checked too: a subject block
    claiming values has to say which years carry them."""
    body = _body(PILOT)
    body["manifest"]["series"][0]["coverage"]["US"] = {
        "first_valid_year": None, "last_valid_year": None,
        "values": 10, "nulls": 0,
    }
    with pytest.raises(ValidationError):
        _parse(body)


def test_declared_absence_round_trips_as_absence_not_zero() -> None:
    """The World Bank holds NO external-debt figure for the US in any year.

    The manifest records it as ``held: false`` with a reason and null years —
    never as a zero a reader could average — and the schema has to carry that
    through typed rather than flattening it.
    """
    desc = _parse(_body(PILOT))
    debt = desc.series_by_id()["wb.external_debt_stocks_usd"]
    assert debt.coverage is not None
    blocks = debt.coverage.subject_blocks()
    assert blocks["US"].held is False
    assert blocks["US"].values == 0
    assert blocks["US"].first_valid_year is None
    assert blocks["US"].last_valid_year is None
    assert "Debtor Reporting System" in (blocks["US"].reason or "")
    assert blocks["IR"].values == 9 and blocks["IR"].last_valid_year == 2024


# ---------------------------------------------------------------------------
# 2. The file convention
# ---------------------------------------------------------------------------


def test_every_committed_collection_yaml_parses() -> None:
    found = sorted(DESCRIPTORS_DIR.glob("collection_*.yaml"))
    assert found, "expected at least the pilot collection manifest"
    for path in found:
        desc = _parse(_body(path))
        assert desc.identity.id, path.name
        assert desc.licence_class in ACCESS_CLASSES, path.name


def test_no_collection_is_named_with_the_source_prefix() -> None:
    """The prefix is load-bearing, not cosmetic.

    ``descriptors/source_*.yaml`` is what the source machinery globs (see
    ``tests/data_pkg/test_source_class_taxonomy.py``), so a collection
    written as ``source_…`` would be activated as a live feed — cadence,
    health, reactive triggers and all — which is the exact failure the whole
    firewall exists to prevent.
    """
    for path in sorted(DESCRIPTORS_DIR.glob("source_*.yaml")):
        body = _body(path)
        assert str(body.get("identity", {}).get("schema_uri", "")).startswith(
            "legba/source/"
        ), f"{path.name} is source_-prefixed but is not a source descriptor"
    for path in sorted(DESCRIPTORS_DIR.glob("collection_*.yaml")):
        assert not path.name.startswith("source_")


def test_document_loader_kinds_are_declared_but_not_loadable_here() -> None:
    """SEAMS #62 — the schema knows the kinds so a manifest can be written
    down and reviewed; the LOADER is what refuses to run one.

    (Refusing at the schema instead would make a document collection
    unwritable, and the seam is "not built", not "not allowed".)
    """
    assert DOCUMENT_LOADER_KINDS == {"documents_wacz", "documents_cc_news"}
    for kind in sorted(DOCUMENT_LOADER_KINDS):
        block = LoaderBlock.model_validate({"kind": kind}, strict=False)
        assert block.kind == kind
        assert block.budget_tokens_per_day == 0


def test_a_bulk_series_under_a_document_loader_kind_refuses() -> None:
    """The cross-check that makes the kinds mean something: a ``bulk_jsonl``
    series is a SERIES fetch, and cannot sit under a document loader."""
    body = _body(PILOT)
    body["loader"]["kind"] = "documents_cc_news"
    with pytest.raises(ValidationError) as exc:
        _parse(body)
    assert "non-series loader kind" in str(exc.value)


# ---------------------------------------------------------------------------
# 3. The lifecycle is the collection's own
# ---------------------------------------------------------------------------


def test_collection_state_machine_is_not_the_shared_one() -> None:
    state_cls, transitions = state_machine_for("collection")
    assert state_cls is CollectionState
    assert transitions is COLLECTION_TRANSITIONS
    assert [s.value for s in CollectionState] == [
        "draft", "reviewed", "loaded", "superseded",
    ]
    assert transitions[CollectionState.SUPERSEDED] == set()
    assert terminal_state_for("collection") is CollectionState.SUPERSEDED
    # …and the four shared-lifecycle families still get the shared one.
    for family in ("target", "analyst", "source", "action_pack"):
        assert state_machine_for(family)[0].__name__ == "LifecycleState"
        assert terminal_state_for(family).value == "retired"


# ---------------------------------------------------------------------------
# 4. The registry, through the real binding path
# ---------------------------------------------------------------------------


def _fixed_identity() -> SigningIdentity:
    seed = b"7g1-collection-registry-seed-det"[:32]
    return SigningIdentity(
        signing_key=SigningKey(seed),
        signer_did="did:legba:registry:7g1-collection",
    )


@pytest_asyncio.fixture
async def api_app(migrated_pg: PostgresConfig):
    os.environ.pop(API_TOKEN_ENV, None)
    pg_store = PostgresStore(migrated_pg)
    await pg_store.connect()

    identity = _fixed_identity()
    audit = AuditLogger(identity=identity)
    dlq = DescriptorDeadLetter(pg_store)
    vocab = VocabularyCache(pg_store)
    vault = CredentialVault(pg_store)
    registry = DescriptorRegistry(
        pg_store, vocabulary_cache=vocab, signing_identity=identity,
        audit_logger=audit, dead_letter=dlq,
    )
    await registry.start()
    deps = RegistryAPIDeps(
        descriptor_registry=registry,
        stack_registry=StackRegistry(pg_store, vault, audit=audit, dlq=dlq),
        vault=vault, dlq=dlq, audit_logger=audit, vocabulary_cache=vocab,
        nats_store=None,
    )
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_router(deps), prefix="/api/v1/registry")

    async with pg_store.acquire() as conn:
        before = {
            r["descriptor_id"]
            for r in await conn.fetch(
                "SELECT descriptor_id FROM collection_descriptors"
            )
        }
    try:
        yield app
    finally:
        try:
            async with pg_store.acquire() as conn:
                await conn.execute(
                    "DELETE FROM collection_descriptors "
                    "WHERE descriptor_id <> ALL($1)",
                    list(before),
                )
        finally:
            await registry.stop()
            await pg_store.close()


@pytest_asyncio.fixture
async def client(api_app):
    async with AsyncClient(
        transport=ASGITransport(app=api_app), base_url="http://testserver",
    ) as c:
        yield c


def _registerable_pilot() -> dict[str, Any]:
    body = _body(PILOT)
    body["identity"]["id"] = f"collection.t7g1_{uuid4().hex[:8]}"
    return body


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collection_family_round_trip_via_http(client: AsyncClient) -> None:
    body = _registerable_pilot()
    desc_id = body["identity"]["id"]

    r = await client.post("/api/v1/registry/descriptors/collection", json=body)
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["family"] == "collection"
    assert row["state"] == "draft"
    assert row["kind"] == "series_api"          # the loader kind, row-level
    assert row["abstraction_level"] == "L0"
    version = row["version"]

    r = await client.get(f"/api/v1/registry/descriptors/collection/{desc_id}")
    assert r.status_code == 200, r.text
    assert r.json()["version"] == version

    r = await client.get(
        f"/api/v1/registry/descriptors/collection/{desc_id}/typed"
    )
    assert r.status_code == 200, r.text
    typed = _parse(r.json())
    assert typed.identity.id == desc_id
    assert typed.manifest_hash() == _parse(body).manifest_hash()
    assert set(typed.firewall.excluded_from) == set(FENCED_SURFACES)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_collection_lifecycle_over_http(client: AsyncClient) -> None:
    body = _registerable_pilot()
    desc_id = body["identity"]["id"]
    r = await client.post("/api/v1/registry/descriptors/collection", json=body)
    assert r.status_code == 201, r.text

    base = f"/api/v1/registry/descriptors/collection/{desc_id}"
    r = await client.post(f"{base}/transition", json={"to_state": "reviewed"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "reviewed"

    r = await client.post(f"{base}/transition", json={"to_state": "loaded"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "loaded"

    # A state from the OTHER families' machine is not a collection state.
    r = await client.post(f"{base}/transition", json={"to_state": "active"})
    assert r.status_code == 400, r.text
    assert "collection" in r.text

    # `superseded` is terminal and goes through /retire, like `retired` does.
    r = await client.post(f"{base}/transition", json={"to_state": "superseded"})
    assert r.status_code == 400, r.text
    r = await client.post(f"{base}/retire", json={"reason": "v2 landed"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "superseded"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unfenced_collection_is_refused_by_the_registry(
    client: AsyncClient,
) -> None:
    """The registry refuses a collection whose firewall omits a surface —
    through the HTTP surface, which is where an operator would meet it."""
    body = _registerable_pilot()
    body["firewall"]["excluded_from"] = [
        s for s in FENCED_SURFACES if s != "surge_detection"
    ]
    r = await client.post("/api/v1/registry/descriptors/collection", json=body)
    assert r.status_code == 422, r.text
    assert "surge_detection" in r.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_family_error_names_collection(client: AsyncClient) -> None:
    r = await client.get("/api/v1/registry/descriptors/holdings/x")
    assert r.status_code == 400
    assert "collection" in r.text
    assert [f.value for f in Family][-1] == "collection"
