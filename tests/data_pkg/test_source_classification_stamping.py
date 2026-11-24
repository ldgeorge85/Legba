# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W10 TAX-2 — source-side classification stamping.

* The tag shape (W8-F1): one regex, bare OR ``<family>:<value>``, the prefix a
  family shape; a declared classification is never bare and never ``src``;
  the new ``scope.classifications`` key is HASH-NEUTRAL when empty.
* The ingest coercion keeps a family-shaped tag verbatim and still flattens a
  bare one (``World News`` -> ``world_news``).
* ``SourceContext.classification`` is built at all FOUR construction sites
  (source_actor, source_validate via the materialiser, manual_batch, entity_gc).
* On the REAL driver: a throwaway ``demo.axis`` family; a declared source is
  refused by name for an unloaded family; a declared source's signal written
  through ``SourceCore._process_one`` carries the declared key in ``tags`` and
  the bound entries (+ its own namespaced tags, an unknown value
  ``unclassified``) in ``data.classifications``; a TAX-1 slice on the key finds
  it; a source with no classifications keeps today's ``tags`` (its legacy
  ``scope.tags`` ride in ``data`` as ``src:``; nothing at all → NULL).
* The four target registrars: flag off, every body byte-identical to the
  pinned registrars (hash fixture); flag on, a region head gains
  ``frame:region`` and the region list is read from the family.
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml
from nacl.signing import SigningKey
from pydantic import ValidationError

from legba.data import tags as tags_mod
from legba.data.config import PostgresConfig
from legba.data.postgres import PostgresStore
from legba.data.registry.descriptor import DescriptorRegistry, Family
from legba.data.registry.errors import DescriptorValidationError
from legba.data.registry.signing import SigningIdentity
from legba.data.schemas import content_hash
from legba.data.schemas.source import SourceDescriptor, SourceScope, SourceSelector, Subscription
from legba.data.schemas.target import TargetDescriptor
from legba.data.source_classification import (
    bind_declared,
    lift_declared_tags,
    preserved_tag,
    signal_classifications,
    signal_data,
)
from legba.data.sources._contract import Signal, SourceContext
from legba.data.sources.baseline import _coerce_tag, run_baseline
from legba.data.tags import (
    STATUS_BOUND,
    STATUS_LEGACY_SRC,
    STATUS_UNCLASSIFIED,
    BoundTag,
    VocabularyIndex,
    install_vocabulary_provider,
)

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DESCRIPTORS = _ROOT / "descriptors"
_SCRIPTS = _ROOT / "scripts"
_GOLDEN = _ROOT / "tests" / "fixtures" / "bringup" / "registrar_bodies_flag_off_2026-10-04.json"
FAMILY = "demo.axis"


def _index() -> VocabularyIndex:
    return VocabularyIndex.from_entries([{"family": FAMILY, "value": "x", "aliases": ["ex"]},
                                         {"family": FAMILY, "value": "OFAC_SDN"}])


@pytest.fixture
def vocab():
    """Install a vocabulary provider for the test; removed after."""
    idx = _index()

    async def _provider() -> VocabularyIndex:
        return idx

    install_vocabulary_provider(_provider)
    yield idx
    install_vocabulary_provider(None)


# ---------------------------------------------------------------------------
# The tag shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tag", [
    "news", "world_news", "demo.axis:x", "demo.axis:OFAC_SDN", "entity_class:person",
    "src:x", "frame:region", "p.axis.sub:v", "crosswalk:geopolitics.lens:x",
])
def test_the_tag_shape_admits_bare_and_namespaced(tag):
    assert SourceScope(tags=[tag]).tags == [tag]
    assert Subscription(tags=[tag]).tags == [tag]
    assert SourceSelector(tags=[tag]).tags == [tag]


@pytest.mark.parametrize(("tag", "why"), [
    ("foo:bar", "'foo' is not a family"),
    ("World", "neither a bare"),
    ("Demo.axis:x", "neither a bare"),
    ("a.b.c.d:x", "neither a bare"),
    ("demo.axis:has space", "neither a bare"),
    ("demo.axis:", "neither a bare"),
    ("x" * 65, "longer than 64"),
])
def test_the_tag_shape_refuses_by_reason(tag, why):
    with pytest.raises(ValidationError) as exc:
        SourceScope(tags=[tag])
    if "longer" not in why and "neither" not in why:
        assert why in str(exc.value)


def test_a_declared_classification_is_namespaced_and_not_src():
    s = SourceScope(classifications=["demo.axis:x", "entity_class:person"])
    assert s.classifications == ["demo.axis:x", "entity_class:person"]
    for bad, why in (("x", "is bare"), ("src:x", "'src' is reserved"), ("foo:bar", "not a family")):
        with pytest.raises(ValidationError) as exc:
            SourceScope(classifications=[bad])
        assert why in str(exc.value)


def test_classifications_is_hash_neutral_when_empty():
    body = yaml.safe_load((_DESCRIPTORS / "source_38north.yaml").read_text())
    plain = SourceDescriptor.model_validate(body, strict=False)
    assert "classifications" not in plain.model_dump(mode="json")["scope"]
    body["scope"]["classifications"] = []
    explicit = SourceDescriptor.model_validate(body, strict=False)
    assert content_hash(explicit) == content_hash(plain)
    body["scope"]["classifications"] = ["demo.axis:x"]
    declared = SourceDescriptor.model_validate(body, strict=False)
    assert declared.model_dump(mode="json")["scope"]["classifications"] == ["demo.axis:x"]
    assert content_hash(declared) != content_hash(plain)


def test_a_target_scope_tag_admits_frame_kind():
    body = {"identity": {"id": "tax2_t", "name": "t", "schema_uri": "legba/target/2.0.0",
                         "version": "0" * 16, "abstraction_level": "L1", "state": "draft",
                         "owner": "test", "created": "2026-10-04T00:00:00Z"},
            "scope": {"domain": "thematic", "themes": [], "tags": ["region", "frame:region"]}}
    assert TargetDescriptor.model_validate(body, strict=False).scope.tags == ["region", "frame:region"]


# ---------------------------------------------------------------------------
# The ingest coercion (W8-F1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "out"), [
    ("demo.axis:x", "demo.axis:x"),
    ("  demo.axis:OFAC_SDN ", "demo.axis:OFAC_SDN"),   # value case kept (bind_tag is exact)
    ("entity_class:person", "entity_class:person"),
    ("src:x", "src:x"),
    ("World News", "world_news"),                      # bare: flattened as before
    ("News: World", "news__world"),                    # not family-shaped: bare, as before
    ("Demo.Axis:X", "demo_axis_x"),                    # prefix not a family shape
    ("demo.axis:has space", "demo_axis_has_space"),    # outside the tag shape
    ("42", None),
    (7, None),
])
def test_coerce_keeps_a_family_shaped_tag_and_flattens_a_bare_one(raw, out):
    assert _coerce_tag(raw) == out


def test_preserved_tag_is_the_shape_rule():
    assert preserved_tag("demo.axis:x") == "demo.axis:x"
    assert preserved_tag("news") is None and preserved_tag("foo:bar") is None


# ---------------------------------------------------------------------------
# Binding, tier-1 lift, the write-time bag (pure)
# ---------------------------------------------------------------------------


def test_bind_declared_puts_classifications_first_and_legacy_tags_as_src():
    scope = SourceScope(classifications=["demo.axis:ex", "demo.nope:y"], tags=["news"])
    got = bind_declared(scope, families=_index())
    assert [(b.family, b.value, b.status) for b in got] == [
        (FAMILY, "x", STATUS_BOUND), ("demo.nope", "y", STATUS_UNCLASSIFIED),
        ("src", "news", STATUS_LEGACY_SRC)]
    # A head body's scope mapping binds the same way; nothing declared → ().
    assert bind_declared(scope.model_dump(mode="json"), families=_index()) == got
    assert bind_declared({}, families=_index()) == () and bind_declared(None, families=_index()) == ()


def test_lift_is_identity_without_a_declared_key():
    t = ["world_news"]
    assert lift_declared_tags(t, ()) is t
    legacy_only = bind_declared({"tags": ["news"]}, families=_index())
    assert lift_declared_tags(t, legacy_only) is t
    declared = bind_declared({"classifications": ["demo.axis:ex"]}, families=_index())
    assert lift_declared_tags(t, declared) == ["demo.axis:x", "world_news"]
    full = [f"t{i}" for i in range(64)]
    assert lift_declared_tags(full, declared)[0] == "demo.axis:x"
    assert len(lift_declared_tags(full, declared)) == 64


async def test_signal_data_bag_shape(vocab):
    declared = bind_declared({"classifications": ["demo.axis:x"], "tags": ["news"]}, families=vocab)
    # Nothing declared and no namespaced tag of its own → NULL.
    assert await signal_data(["world_news"], ()) is None
    assert await signal_data([], ()) is None
    bag = await signal_data(["demo.axis:x", "world_news", "demo.axis:nope", "src:z"], declared)
    assert bag == {"classifications": [
        {"family": FAMILY, "value": "x", "status": STATUS_BOUND},
        {"family": "src", "value": "news", "status": STATUS_LEGACY_SRC},
        {"family": FAMILY, "value": "nope", "status": STATUS_UNCLASSIFIED, "raw": "demo.axis:nope"},
    ]}
    assert signal_classifications((), ["entity_class:person"], families=vocab) == [
        {"family": "entity_class", "value": "person", "status": STATUS_UNCLASSIFIED,
         "raw": "entity_class:person"}]


async def test_run_baseline_lifts_beside_the_scope_geo_lift(vocab):
    ctx = _ctx(classification=bind_declared({"classifications": ["demo.axis:x"]}, families=vocab),
               scope_geo=["KP"])
    sig = Signal(source_id="s", payload={"title": "t", "tags": ["World News", "demo.axis:OFAC_SDN"]})
    out = await run_baseline(sig, ctx)
    assert out.tags == ["demo.axis:x", "world_news", "demo.axis:OFAC_SDN"]
    assert out.payload["publisher_origin"] == ["KP"]
    plain = await run_baseline(Signal(source_id="s", payload={"tags": ["World News"]}), _ctx())
    assert plain.tags == ["world_news"]


def _ctx(**kw: Any) -> SourceContext:
    from legba.data.sources._contract import InMemoryStateStore
    from pydantic import BaseModel

    class _C(BaseModel):
        pass

    return SourceContext(target_id="s", target_version="v", source_id="s", config=_C(),
                         state_store=InMemoryStateStore(), **kw)


# ---------------------------------------------------------------------------
# The four construction sites
# ---------------------------------------------------------------------------


def _source_body(sid: str, *, classifications: list[str] | None = None, state: str = "draft") -> dict:
    body = yaml.safe_load((_DESCRIPTORS / "source_38north.yaml").read_text())
    body["identity"].update({"id": sid, "version": "0" * 16, "state": state})
    if classifications is not None:
        body["scope"]["classifications"] = classifications
    return body


def _expected(families: VocabularyIndex) -> tuple[BoundTag, ...]:
    return bind_declared(_source_body("x", classifications=["demo.axis:ex"])["scope"], families=families)


async def test_site_1_source_actor_make_context(vocab):
    from legba.runtime.source_actor import SourceCore, SourceDeps

    sd = SourceDescriptor.model_validate(
        _source_body("source.test.tax2_site", classifications=["demo.axis:ex"]), strict=False)
    core = SourceCore("a", SourceDeps(descriptor=sd, deps=SimpleNamespace(pg_pool=None, secrets_resolve=None)))
    before = core._make_context().classification            # no vocabulary read yet: EMPTY
    assert [b.status for b in before][:1] == [STATUS_UNCLASSIFIED]
    core._families = await tags_mod.tag_vocabulary_index()  # what pull_once/_emit do first
    assert core._make_context().classification == _expected(vocab)
    assert core._make_context().classification[0].key == "demo.axis:x"


async def test_site_2_source_validate_via_the_materializer(vocab, monkeypatch):
    from legba.data.discovery import source_materializer as sm
    from legba.data.discovery import source_validate as sv
    from legba.data.discovery.source_contract import CandidateSource, SourceCandidateValidation

    seen: dict[str, Any] = {}

    class _Handler:
        async def health_check(self, ctx):
            seen["ctx"] = ctx
            return SimpleNamespace(state="unhealthy")

    cand = CandidateSource(id="https://example.invalid/feed", source_kind="rss")
    cls = _expected(vocab)
    await sv.validate_candidate_source(cand, handler=_Handler(), classification=cls)
    assert seen["ctx"].classification == cls

    async def _fake_validate(candidate, **kw):
        seen["kw"] = kw
        return SourceCandidateValidation(natural_key=candidate.natural_key, valid=False,
                                         live=False, reason="probe")

    monkeypatch.setattr(sm, "validate_candidate_source", _fake_validate)
    disc = SourceDescriptor.model_validate(_source_body(
        "source.test.tax2_disc", classifications=["demo.axis:ex"]) | {}, strict=False)
    out = await sm.materialize_discovered_source(None, cand, disc, rules=[])
    assert out.rejected and seen["kw"]["classification"] == cls


async def test_site_3_manual_batch(vocab, monkeypatch):
    from legba.data.seed import manual_batch as mb
    from legba.data.seed.manual_schema import ManualSignalRecord

    seen: dict[str, Any] = {}

    async def _baseline(sig, ctx, **kw):
        seen["ctx"] = ctx
        return None  # a filter drop → SKIP, no write

    monkeypatch.setattr(mb, "run_baseline", _baseline)
    rec = ManualSignalRecord(title="t", published_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    action = await mb._apply_signal(
        None, rec, manifest=SimpleNamespace(batch_id="b", operator="op"), source_id="source.manual.b",
        fetched_at=datetime.now(tz=timezone.utc), enrichment_stage=None, publish_signal=None,
        report=None)
    assert action == mb.RecordAction.SKIP
    assert seen["ctx"].classification == bind_declared(mb._MANUAL_SOURCE_SCOPE, families=vocab)
    assert [b.key for b in seen["ctx"].classification] == ["src:manual", "src:backfill"]


async def test_site_4_entity_gc_reprobe(vocab, monkeypatch):
    from legba.data.analysts.deterministic_handlers import entity_gc as eg

    seen: dict[str, Any] = {}
    monkeypatch.setattr(eg, "build_source_handler", lambda *a, **k: object())

    async def _fresh(*, handler, ctx, auto_paused_at):
        seen["ctx"] = ctx
        return None

    monkeypatch.setattr(eg, "_probe_content_freshness", _fresh)
    body = _source_body("source.test.tax2_gc", classifications=["demo.axis:ex"])
    out = await eg._probe_source_health(descriptor_id="source.test.tax2_gc", kind="rss",
                                        body=body, deps=None)
    assert out["reason"] == "no_parseable_item_dates"
    assert seen["ctx"].classification == _expected(vocab)


# ---------------------------------------------------------------------------
# The REAL driver
# ---------------------------------------------------------------------------


def _identity() -> SigningIdentity:
    return SigningIdentity(signing_key=SigningKey(b"w10-tax2-source-classification-0"[:32]),
                           signer_did="did:legba:registry:tax2-test")


async def _scrub(conn) -> None:
    await conn.execute("DELETE FROM signal_aliases WHERE alias_signal_id IN "
                       "(SELECT id FROM signals WHERE source_id LIKE 'source.test.tax2%')")
    await conn.execute("DELETE FROM signals WHERE source_id LIKE 'source.test.tax2%'")
    await conn.execute("DELETE FROM source_descriptors WHERE descriptor_id LIKE 'source.test.tax2%'")
    await conn.execute("DELETE FROM target_descriptors WHERE descriptor_id LIKE 'tax2\\_%'")
    await conn.execute(
        "DELETE FROM descriptor_dead_letter "
        "WHERE attempted_payload->'identity'->>'id' LIKE 'source.test.tax2%' "
        "   OR attempted_payload->'identity'->>'id' LIKE 'tax2\\_%'")
    await conn.execute("DELETE FROM vocabulary_entries WHERE family = $1", FAMILY)


@pytest_asyncio.fixture
async def world(migrated_pg: PostgresConfig):
    store = PostgresStore(migrated_pg)
    await store.connect()
    async with store.acquire() as conn:
        await _scrub(conn)
        await conn.execute(
            "INSERT INTO vocabulary_entries (family, value, aliases) VALUES ($1, 'x', $2)",
            FAMILY, ["ex"])
    identity = _identity()
    from legba.data.registry.audit import AuditLogger

    reg = DescriptorRegistry(store, signing_identity=identity, audit_logger=AuditLogger(identity=identity))
    await reg.start()  # the vocabulary cache now has demo.axis loaded

    async def _provider() -> VocabularyIndex:
        return VocabularyIndex.from_cache(reg._vocab)

    install_vocabulary_provider(_provider)
    yield SimpleNamespace(store=store, reg=reg)
    install_vocabulary_provider(None)
    await reg.stop()
    async with store.acquire() as conn:
        await _scrub(conn)
    await store.close()


async def _register_source(w, *, classifications: list[str] | None) -> SourceDescriptor:
    sid = f"source.test.tax2_{uuid4().hex[:10]}"
    await w.reg.register(SourceDescriptor.model_validate(
        _source_body(sid, classifications=classifications), strict=False), "test")
    head = await w.reg.get(sid, family=Family.SOURCE)
    return SourceDescriptor.model_validate(head.body, strict=False)


@pytest.mark.integration
async def test_an_unloaded_family_is_refused_by_name_at_registration(world):
    with pytest.raises(DescriptorValidationError) as exc:
        await _register_source(world, classifications=["demo.nope:x"])
    assert "demo.nope" in str(exc.value) and exc.value.validation_error["family"] == "family"
    with pytest.raises(DescriptorValidationError) as exc2:
        await _register_source(world, classifications=[f"{FAMILY}:zzz"])
    assert exc2.value.validation_error["family"] == FAMILY
    assert exc2.value.validation_error["unknown_values"] == ["zzz"]
    async with world.store.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM source_descriptors WHERE descriptor_id LIKE 'source.test.tax2%'")
    assert n == 0
    ok = await _register_source(world, classifications=[f"{FAMILY}:ex"])
    assert ok.scope.classifications == [f"{FAMILY}:ex"]          # persisted, read back


async def _write_through_core(w, sd: SourceDescriptor, payload_tags: list[str]) -> tuple[Any, ...]:
    from legba.runtime.source_actor import SourceCore, SourceDeps

    core = SourceCore("tax2", SourceDeps(descriptor=sd, deps=SimpleNamespace(pg_pool=None,
                                                                              secrets_resolve=None)))
    core._families = await tags_mod.tag_vocabulary_index()
    ctx = core._make_context()
    url = f"https://example.invalid/tax2/{uuid4().hex}"
    raw = Signal(source_id=sd.identity.id, canonical_url=url,
                 payload={"title": f"tax2 {uuid4().hex}", "tags": payload_tags})
    async with w.store.acquire() as conn:
        written = await core._process_one(conn, ctx, raw)
        row = await conn.fetchrow("SELECT tags, data FROM signals WHERE id = $1", written.signal_id)
    data = row["data"]
    return written, list(row["tags"]), (json.loads(data) if isinstance(data, str) else data)


@pytest.mark.integration
async def test_a_declared_source_stamps_every_signal_and_the_slice_finds_it(world):
    from legba.runtime.actor_substrate_slice import _read_substrate_slice

    declared = await _register_source(world, classifications=[f"{FAMILY}:ex"])
    plain = await _register_source(world, classifications=None)
    sig, tags, data = await _write_through_core(
        world, declared, ["World News", f"{FAMILY}:nope"])
    # signals.tags: the declared key first, the bare tag flattened, the
    # family-shaped one intact.
    assert tags == [f"{FAMILY}:x", "world_news", f"{FAMILY}:nope"]
    assert data == {"classifications": [
        {"family": FAMILY, "value": "x", "status": STATUS_BOUND},
        *[{"family": "src", "value": t, "status": STATUS_LEGACY_SRC} for t in declared.scope.tags],
        {"family": FAMILY, "value": "nope", "status": STATUS_UNCLASSIFIED, "raw": f"{FAMILY}:nope"},
    ]}
    # A source with no scope.classifications: tags exactly as before (flattened,
    # nothing lifted); data carries only its legacy scope.tags as `src:`.
    _, ptags, pdata = await _write_through_core(world, plain, ["World News"])
    assert ptags == ["world_news"]
    assert pdata == {"classifications": [
        {"family": "src", "value": t, "status": STATUS_LEGACY_SRC} for t in plain.scope.tags]}
    # The TAX-1 slice on the declared key: the declared source's signal is in it.
    tid = f"tax2_t_{uuid4().hex[:10]}"
    await world.reg.register(TargetDescriptor.model_validate({
        "identity": {"id": tid, "name": tid, "schema_uri": "legba/target/2.0.0",
                     "version": "0" * 16, "abstraction_level": "L1", "state": "draft",
                     "owner": "test", "created": "2026-10-04T00:00:00Z"},
        "scope": {"domain": "thematic", "themes": [], "signal_tags": [f"{FAMILY}:x"]},
        "sources": [{"source_id": declared.identity.id}, {"source_id": plain.identity.id}],
    }, strict=False), "test")
    probe = SimpleNamespace(identity=SimpleNamespace(id="tax2_probe", kind="predictor"),
                            subscription=SimpleNamespace(substrate={},
                                                         targets=SimpleNamespace(time_window="24h")))
    async with world.store.acquire() as conn:
        rows = await _read_substrate_slice(conn, descriptor=probe, target_filter=tid)
    assert {str(r["id"]) for r in rows if r.get("id") is not None} == {str(sig.signal_id)}


@pytest.mark.integration
async def test_a_family_shaped_payload_tag_reaches_signals_tags_intact(world):
    plain = await _register_source(world, classifications=None)
    _, tags, data = await _write_through_core(world, plain, [f"{FAMILY}:x", "World News"])
    assert tags == [f"{FAMILY}:x", "world_news"]
    assert data["classifications"][-1] == {"family": FAMILY, "value": "x", "status": STATUS_BOUND}


# ---------------------------------------------------------------------------
# The registrars: flag off byte-identical, flag on frame-kind aware
# ---------------------------------------------------------------------------


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


@pytest.fixture
def registrars(monkeypatch):
    monkeypatch.delenv("LEGBA_FRAME_FAMILY", raising=False)
    import os

    had = "LEGBA_DATA_PG_DB" in os.environ
    sys.path.insert(0, str(_SCRIPTS))
    try:
        import bringup_register_g20_country_targets as g20
        import bringup_register_region_targets as region
        import bringup_register_situation_targets as situation
        import bringup_register_watch_country_targets as watch
        import _frame_kind as fk
    finally:
        if not had:
            os.environ.pop("LEGBA_DATA_PG_DB", None)
    return SimpleNamespace(g20=g20, watch=watch, region=region, situation=situation, fk=fk)


def _bodies(r, catalog=None) -> dict[str, dict[str, Any]]:
    return {
        "g20": {c: r.g20._build_target(c, f"Name {c}", ["xx"], catalog).model_dump(mode="json")
                for c in r.g20.G20_ISO2},
        "watch": {c: r.watch._build_target(c, f"Name {c}", ["xx"], catalog).model_dump(mode="json")
                  for c in r.watch.WATCH_ISO2},
        "region": {i: b for i, b, _ in r.region.build_region_frames(catalog)},
        "situation": {f: r.situation._load(f, catalog).model_dump(mode="json")
                      for f in r.situation.SITUATION_TARGET_FILES},
    }


def test_registrars_flag_off_are_byte_identical_to_the_pin(registrars):
    golden = json.loads(_GOLDEN.read_text())["bodies"]
    got = {g: {k: _sha(v) for k, v in sorted(b.items())} for g, b in _bodies(registrars).items()}
    assert got == golden
    # Flag off with a LOADED catalog changes nothing either.
    cat = registrars.fk.tree_catalog()
    assert cat.loaded
    got2 = {g: {k: _sha(v) for k, v in sorted(b.items())}
            for g, b in _bodies(registrars, cat).items()}
    assert got2 == golden


def test_registrars_flag_on_tag_frame_heads_and_read_the_family(registrars, monkeypatch):
    monkeypatch.setenv("LEGBA_FRAME_FAMILY", "1")
    cat = registrars.fk.tree_catalog()
    bodies = _bodies(registrars, cat)
    region_ids = sorted(f.id for f in cat.groups("region"))
    assert sorted(bodies["region"]) == region_ids
    for rid, body in bodies["region"].items():
        assert body["scope"]["tags"] == ["region", rid, "geopolitical", "frame:region"]
    # A desk or a situation head that is not a registered frame is unchanged.
    golden = json.loads(_GOLDEN.read_text())["bodies"]
    for g in ("g20", "watch", "situation"):
        assert {k: _sha(v) for k, v in sorted(bodies[g].items())} == golden[g]
    # An unloaded catalog changes nothing even with the flag on.
    off = registrars.fk.catalog_from_rows([{"value": "bad", "aliases": ["nope:x"], "notes": "n"},
                                           {"value": 7}])
    assert not off.loaded
    assert registrars.fk.frame_kind_tags("region_europe", ["region"], off) == ["region"]


def test_roster_tag_is_read_from_the_profile(registrars):
    assert registrars.fk.roster_tag("g20") == "g20"
    with pytest.raises(ValueError) as exc:
        registrars.fk.roster_tag("not_a_roster_tag")
    assert "not_a_roster_tag" in str(exc.value)


# ---------------------------------------------------------------------------
# The leaf
# ---------------------------------------------------------------------------


def test_source_classification_is_a_leaf():
    """stdlib + ``.tags`` only — ``schemas/source.py`` imports it."""
    tree = ast.parse((_ROOT / "src/legba/data/source_classification.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert (node.level, node.module) in {(0, "__future__"), (0, "collections.abc"),
                                                 (0, "typing"), (1, "tags")}, node.module
        elif isinstance(node, ast.Import):
            assert [a.name for a in node.names] == ["re"]
