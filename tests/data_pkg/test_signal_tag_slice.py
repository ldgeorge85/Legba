# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W8 TAX-1 — the slice side of the tag spine.

* The legacy equivalence rule (`tags.signal_tag_match_forms`): a bare ``x`` and
  ``src:x`` are one tag; ``demo.axis:x`` is another — proven as the exact
  preimage of `signal_tag_key`.
* `SignalTagFilter`: the SQL clauses and their Python mirror agree.
* The schema keys: ``scope.signal_tags`` / ``scope.exclude_signal_tags`` and
  ``subscription.substrate.signal_tags`` / ``untagged_fallback``.
* The ``slice_per_source_cap`` knob: declared in the kind catalog, validated,
  strict; absent ⇒ the reader's SQL and row set are what they were.
* On the REAL driver: a throwaway ``demo.axis`` family registered, a source
  registered, signals written, targets and analysts registered through the
  real ``DescriptorRegistry`` (which refuses an unloaded family by name), and
  both slice readers read back. Everything registered is scrubbed at teardown.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml
from nacl.signing import SigningKey

from legba.data.analysts.cross_target_raw import READ_SLICE, read_cross_target_slice
from legba.data.config import PostgresConfig
from legba.data.option_catalog.handler_options import (
    ANALYST_KIND_OPTIONS,
    known_kind_option_names,
    resolve_kind_options,
)
from legba.data.postgres import PostgresStore
from legba.data.registry.descriptor import DescriptorRegistry, Family
from legba.data.registry.errors import DescriptorValidationError
from legba.data.registry.signing import SigningIdentity
from legba.data.schemas.analyst import AnalystDescriptor
from legba.data.schemas.source import SourceDescriptor
from legba.data.schemas.target import TargetDescriptor
from legba.data.signal_slice_scope import (
    CANONICAL_ONLY_SQL,
    NO_FILTER,
    SignalTagFilter,
    analyst_tag_filter,
    declared_signal_tags,
    signal_tag_refusals,
    slice_per_source_cap,
    strict_per_source_cap,
    target_tag_filter,
)
from legba.data.tags import VocabularyIndex, signal_tag_key, signal_tag_match_forms
from legba.runtime.actor_substrate_slice import _read_substrate_slice

_ROOT = Path(__file__).resolve().parents[2]
_DESCRIPTORS = _ROOT / "descriptors"
FAMILY = "demo.axis"


# ---------------------------------------------------------------------------
# The legacy equivalence rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("tag", "forms"), [
    ("demo.axis:x", ("demo.axis:x",)),
    ("x", ("src:x", "x")),
    ("src:x", ("src:x", "x")),
    ("src:demo.axis:x", ("src:demo.axis:x",)),
    ("News: World", ("src:News: World", "News: World")),
    ("entity_class:person", ("entity_class:person",)),
    ("", ()),
    (None, ()),
])
def test_match_forms_are_the_documented_ones(tag, forms):
    assert signal_tag_match_forms(tag) == forms


_STORED = [
    "x", "src:x", "demo.axis:x", "demo.axis:y", "y", "src:y", "other.axis:x",
    "src:demo.axis:x", "News: World", "src:News: World", "severity:high",
    "entity_class:person", "person", "sport", "src:sport", "أخبار",
]


@pytest.mark.parametrize("declared", _STORED)
def test_match_forms_are_exactly_the_preimage_of_the_key(declared):
    """A stored tag matches a declared one IFF `has_tag()`'s key says they are
    the same tag — the slice and the predicate engine cannot disagree."""
    forms = set(signal_tag_match_forms(declared))
    for stored in _STORED:
        assert (stored in forms) == (signal_tag_key(stored) == signal_tag_key(declared)), (
            declared, stored)


def test_a_bare_x_is_not_the_namespaced_x():
    assert "x" not in signal_tag_match_forms("demo.axis:x")
    assert "demo.axis:x" not in signal_tag_match_forms("x")


# ---------------------------------------------------------------------------
# The filter: SQL and its Python mirror
# ---------------------------------------------------------------------------


def test_filter_sql_numbers_after_existing_params_and_shapes_each_clause():
    params: list[Any] = [72, ["s1"]]
    f = SignalTagFilter(include=("demo.axis:x",), exclude=("src:sport", "sport"),
                        untagged_fallback=True)
    assert f.sql(params) == [
        "(tags && $3::text[] OR tags = '{}')",
        "NOT (tags && $4::text[])",
    ]
    assert params[2:] == [["demo.axis:x"], ["src:sport", "sport"]]
    strict = SignalTagFilter(include=("a",))
    assert strict.sql([]) == ["tags && $1::text[]"]
    assert NO_FILTER.sql(params) == [] and len(params) == 4


@pytest.mark.parametrize(("tags", "strict", "fallback"), [
    (["demo.axis:x"], True, True),
    (["demo.axis:x", "sport"], False, False),
    ([], False, True),
    (["x"], False, False),
    (["news"], False, False),
])
def test_admits_mirrors_the_sql(tags, strict, fallback):
    inc, exc = ("demo.axis:x",), ("src:sport", "sport")
    assert SignalTagFilter(include=inc, exclude=exc).admits(tags) is strict
    assert SignalTagFilter(include=inc, exclude=exc, untagged_fallback=True).admits(tags) is fallback


def test_target_and_analyst_filters_read_their_declarations():
    t = target_tag_filter({"signal_tags": ["x"], "exclude_signal_tags": ["demo.axis:y"]})
    assert t == SignalTagFilter(include=("src:x", "x"), exclude=("demo.axis:y",))
    assert target_tag_filter({"geo": ["BR"]}) == NO_FILTER
    assert target_tag_filter(None) == NO_FILTER

    def _a(sub):
        return SimpleNamespace(subscription=SimpleNamespace(substrate=sub))

    assert analyst_tag_filter(_a({"signal_tags": ["demo.axis:x"]})).untagged_fallback is True
    assert analyst_tag_filter(_a({"signal_tags": ["demo.axis:x"],
                                  "untagged_fallback": False})).untagged_fallback is False
    assert analyst_tag_filter(_a({"direct_queries": False})) == NO_FILTER
    assert analyst_tag_filter(SimpleNamespace()) == NO_FILTER


# ---------------------------------------------------------------------------
# Registration-time refusal (pure) and the schema keys
# ---------------------------------------------------------------------------


def _index() -> VocabularyIndex:
    return VocabularyIndex.from_entries([{"family": FAMILY, "value": "x", "aliases": ["ex"]}])


def test_refusals_name_the_unloaded_family_then_the_unknown_value():
    assert signal_tag_refusals(["demo.axis:x", "demo.axis:ex", "x", "src:sport"], _index()) == ([], {})
    assert signal_tag_refusals(["demo.nope:x"], _index()) == (["demo.nope"], {})
    assert signal_tag_refusals(["demo.axis:zzz"], _index()) == ([], {FAMILY: ["zzz"]})
    # `News: World` is bare (no family-shaped prefix) → legacy, admissible.
    assert signal_tag_refusals(["News: World"], _index()) == ([], {})


def _target_body(tid: str, *, scope_extra: dict | None = None, sources: list[str] | None = None,
                 state: str = "draft") -> dict[str, Any]:
    return {
        "identity": {
            "id": tid, "name": tid, "schema_uri": "legba/target/2.0.0", "version": "0" * 16,
            "abstraction_level": "L1", "state": state, "owner": "test",
            "created": "2026-10-04T00:00:00Z",
        },
        "scope": {"domain": "thematic", "themes": [], **(scope_extra or {})},
        "sources": [{"source_id": s} for s in (sources or [])],
    }


def test_target_scope_carries_the_two_keys_defaulted_empty():
    t = TargetDescriptor.model_validate(_target_body("tax1_shape"), strict=False)
    assert t.scope.signal_tags == [] and t.scope.exclude_signal_tags == []
    t2 = TargetDescriptor.model_validate(_target_body("tax1_shape", scope_extra={
        "signal_tags": ["demo.axis:x"], "exclude_signal_tags": ["sport"]}), strict=False)
    assert declared_signal_tags(t2) == ["demo.axis:x", "sport"]
    with pytest.raises(Exception):
        TargetDescriptor.model_validate(_target_body("tax1_shape", scope_extra={
            "signal_tags": [" padded"]}), strict=False)


def _analyst_body(aid: str, *, substrate: dict | None = None, options: dict | None = None,
                  state: str = "draft") -> dict[str, Any]:
    body = yaml.safe_load((_DESCRIPTORS / "analyst_escalation.yaml").read_text())
    body["identity"].update({"id": aid, "version": "0" * 16, "state": state})
    if substrate is not None:
        body["subscription"]["substrate"] = substrate
    if options is not None:
        body["method"]["options"] = options
    return body


def test_analyst_substrate_keys_are_type_checked():
    ok = AnalystDescriptor.model_validate(_analyst_body("tax1_shape", substrate={
        "direct_queries": False, "signal_tags": ["demo.axis:x"], "untagged_fallback": False,
    }), strict=False)
    assert declared_signal_tags(ok) == ["demo.axis:x"]
    for bad in ({"signal_tags": "demo.axis:x"}, {"signal_tags": [""]},
                {"signal_tags": ["x"], "untagged_fallback": "yes"}):
        with pytest.raises(Exception):
            AnalystDescriptor.model_validate(_analyst_body("tax1_shape", substrate=bad), strict=False)


# ---------------------------------------------------------------------------
# The per-source cap knob
# ---------------------------------------------------------------------------


def test_the_cap_knob_is_declared_for_both_slice_kinds():
    for kind in ("inline_target", "cross_target_raw"):
        assert "slice_per_source_cap" in known_kind_option_names(kind)
        res = resolve_kind_options(kind, {"slice_per_source_cap": 3})
        assert res.accepted == {"slice_per_source_cap": 3} and not res.rejected
    assert resolve_kind_options("inline_target", {"slice_per_source_cap": 0}).rejected
    assert "cross_target_raw" in ANALYST_KIND_OPTIONS


def _desc(kind: str, options: dict | None) -> SimpleNamespace:
    return SimpleNamespace(identity=SimpleNamespace(id="a", kind=kind),
                           method=SimpleNamespace(options=options))


@pytest.mark.parametrize(("kind", "options", "cap"), [
    ("inline_target", {"slice_per_source_cap": 2}, 2),
    ("cross_target_raw", {"slice_per_source_cap": 1}, 1),
    ("inline_target", {}, None),
    ("inline_target", None, None),
    ("inline_target", {"slice_per_source_cap": 0}, None),
    ("inline_target", {"slice_per_source_cap": True}, None),
    ("predictor", {"slice_per_source_cap": 2}, None),
])
def test_slice_per_source_cap_resolution(kind, options, cap):
    assert slice_per_source_cap(_desc(kind, options)) == cap


def test_strict_cap_never_back_fills():
    rows = [{"source_id": s} for s in ("a", "a", "a", "b", "a")]
    assert [r["source_id"] for r in strict_per_source_cap(rows, per_source_cap=1, limit=10)] == ["a", "b"]
    assert len(strict_per_source_cap(rows, per_source_cap=5, limit=3)) == 3


# ---------------------------------------------------------------------------
# Byte-identity of an undeclared slice (built SQL, no DB)
# ---------------------------------------------------------------------------


class _Capture:
    def __init__(self, bodies: dict[str, dict]) -> None:
        self.bodies = bodies
        self.sqls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *a):
        return {"body": self.bodies.get(a[0], {})} if a else None

    async def fetch(self, sql, *params):
        if "FROM signals" in sql:
            self.sqls.append((sql, params))
        return []


async def test_an_undeclared_unit_slice_carries_no_tag_clause():
    conn = _Capture({"t": {"scope": {"geo": ["BR"]}}})
    desc = SimpleNamespace(identity=SimpleNamespace(id="p", kind="predictor"),
                           subscription=SimpleNamespace(substrate={}, targets=None))
    await _read_substrate_slice(conn, descriptor=desc, target_filter="t")
    sql = conn.sqls[0][0]
    assert "tags &&" not in sql and CANONICAL_ONLY_SQL in sql and "LIMIT 360" in sql


async def test_an_undeclared_cross_slice_is_the_union_shape():
    conn = _Capture({"t1": {"scope": {"geo": ["US"]}}, "t2": {"sources": [{"source_id": "s"}]}})
    await read_cross_target_slice(conn, target_ids=["t1", "t2"])
    sql, params = conn.sqls[0]
    assert "(source_id = ANY($2::TEXT[]) OR geo && $3::text[])" in sql
    assert "tags" not in sql.split("WHERE", 1)[1] and "LIMIT 200" in sql
    assert params == (24, ["s"], ["US"])


# ---------------------------------------------------------------------------
# The REAL driver: register, write, read the slice back
# ---------------------------------------------------------------------------


def _identity() -> SigningIdentity:
    return SigningIdentity(signing_key=SigningKey(b"w8-tax1-signal-tag-slice-seed-0123"[:32]),
                           signer_did="did:legba:registry:tax1-test")


async def _scrub(conn) -> None:
    await conn.execute("DELETE FROM signals WHERE source_id LIKE 'source.test.tax1%'")
    await conn.execute("DELETE FROM source_descriptors WHERE descriptor_id LIKE 'source.test.tax1%'")
    await conn.execute("DELETE FROM target_descriptors WHERE descriptor_id LIKE 'tax1\\_%'")
    await conn.execute("DELETE FROM analyst_descriptors WHERE descriptor_id LIKE 'tax1\\_%'")
    await conn.execute(
        "DELETE FROM descriptor_dead_letter "
        "WHERE attempted_payload->'identity'->>'id' LIKE 'tax1\\_%'")
    await conn.execute("DELETE FROM vocabulary_entries WHERE family = $1", FAMILY)


async def _source(reg: DescriptorRegistry) -> str:
    body = yaml.safe_load((_DESCRIPTORS / "source_38north.yaml").read_text())
    sid = f"source.test.tax1_{uuid4().hex[:10]}"
    body["identity"].update({"id": sid, "version": "0" * 16, "state": "draft"})
    await reg.register(SourceDescriptor.model_validate(body, strict=False), "test")
    return sid


async def _signal(conn, source_id: str, tags: list[str]) -> str:
    sid = uuid4()
    now = datetime.now(tz=timezone.utc)
    await conn.execute(
        "INSERT INTO signals (id, source_id, owner_tenant, modality, payload, content_hash, "
        "fetched_at, created_at, tags) VALUES ($1,$2,'default','text',$3::jsonb,$4,$5,$5,$6::text[])",
        sid, source_id, json.dumps({"title": f"tax1 {tags}"}), f"h_{uuid4().hex}", now, tags,
    )
    return str(sid)


class World(SimpleNamespace):
    pass


@pytest_asyncio.fixture
async def world(migrated_pg: PostgresConfig):
    store = PostgresStore(migrated_pg)
    await store.connect()
    async with store.acquire() as conn:
        await _scrub(conn)
        await conn.execute(
            "INSERT INTO vocabulary_entries (family, value, aliases) VALUES ($1, 'x', $2)",
            FAMILY, ["ex"],
        )
    identity = _identity()
    from legba.data.registry.audit import AuditLogger

    reg = DescriptorRegistry(store, signing_identity=identity, audit_logger=AuditLogger(identity=identity))
    await reg.start()  # refreshes the vocabulary cache: demo.axis is loaded
    src_a, src_b = await _source(reg), await _source(reg)
    async with store.acquire() as conn:
        ids = {
            "tagged": await _signal(conn, src_a, ["demo.axis:x"]),
            "untagged": await _signal(conn, src_a, []),
            "legacy": await _signal(conn, src_a, ["x"]),
            "src": await _signal(conn, src_a, ["src:x"]),
            "sport": await _signal(conn, src_a, ["sport"]),
            "b1": await _signal(conn, src_b, ["demo.axis:x"]),
            "b2": await _signal(conn, src_b, []),
        }
    w = World(store=store, reg=reg, a=src_a, b=src_b, ids=ids)
    yield w
    await reg.stop()
    async with store.acquire() as conn:
        await _scrub(conn)
    await store.close()


async def _target(w: World, *, sources: list[str], **scope: Any) -> str:
    tid = f"tax1_t_{uuid4().hex[:10]}"
    await w.reg.register(TargetDescriptor.model_validate(
        _target_body(tid, scope_extra=scope, sources=sources), strict=False), "test")
    return tid


def _probe(substrate: dict | None = None) -> SimpleNamespace:
    """A non-inline kind, so the desk-grounding leg stays out of the way."""
    return SimpleNamespace(identity=SimpleNamespace(id="tax1_probe", kind="predictor"),
                           subscription=SimpleNamespace(substrate=substrate or {},
                                                        targets=SimpleNamespace(time_window="24h")))


async def _unit_ids(w: World, tid: str, descriptor: Any) -> set[str]:
    async with w.store.acquire() as conn:
        rows = await _read_substrate_slice(conn, descriptor=descriptor, target_filter=tid)
    inv = {v: k for k, v in w.ids.items()}
    return {inv[str(r["id"])] for r in rows if r.get("id") is not None and str(r["id"]) in inv}


@pytest.mark.integration
async def test_a_namespaced_tag_round_trips_into_the_slice(world: World):
    tid = await _target(world, sources=[world.a], signal_tags=[f"{FAMILY}:x"])
    head = await world.reg.get(tid, family=Family.TARGET)
    assert head.body["scope"]["signal_tags"] == [f"{FAMILY}:x"]  # persisted, read back
    assert await _unit_ids(world, tid, _probe()) == {"tagged"}


@pytest.mark.integration
async def test_exclude_takes_it_out(world: World):
    tid = await _target(world, sources=[world.a], exclude_signal_tags=[f"{FAMILY}:x"])
    assert await _unit_ids(world, tid, _probe()) == {"untagged", "legacy", "src", "sport"}
    both = await _target(world, sources=[world.a], signal_tags=[f"{FAMILY}:x", "x"],
                         exclude_signal_tags=[f"{FAMILY}:x"])
    assert await _unit_ids(world, both, _probe()) == {"legacy", "src"}


@pytest.mark.integration
async def test_an_untagged_signal_appears_only_under_the_fallback(world: World):
    tid = await _target(world, sources=[world.a])
    fallback = AnalystDescriptor.model_validate(_analyst_body(
        "tax1_fallback", substrate={"direct_queries": False, "signal_tags": [f"{FAMILY}:x"]},
        options={}), strict=False)
    strict = AnalystDescriptor.model_validate(_analyst_body(
        "tax1_strict", substrate={"direct_queries": False, "signal_tags": [f"{FAMILY}:x"],
                                  "untagged_fallback": False}, options={}), strict=False)
    assert await _unit_ids(world, tid, fallback) == {"tagged", "untagged"}
    assert await _unit_ids(world, tid, strict) == {"tagged"}
    # The target's own include is strict whatever the analyst falls back to.
    scoped = await _target(world, sources=[world.a], signal_tags=[f"{FAMILY}:x"])
    assert await _unit_ids(world, scoped, _probe({"signal_tags": [f"{FAMILY}:x"]})) == {"tagged"}


@pytest.mark.integration
async def test_the_legacy_bare_form_matches_src_and_not_the_namespaced_tag(world: World):
    bare = await _target(world, sources=[world.a], signal_tags=["x"])
    assert await _unit_ids(world, bare, _probe()) == {"legacy", "src"}
    src = await _target(world, sources=[world.a], signal_tags=["src:x"])
    assert await _unit_ids(world, src, _probe()) == {"legacy", "src"}


@pytest.mark.integration
async def test_an_unknown_family_is_refused_by_name_at_registration(world: World):
    with pytest.raises(DescriptorValidationError) as exc:
        await _target(world, sources=[world.a], signal_tags=["demo.nope:x"])
    assert "demo.nope" in str(exc.value) and exc.value.validation_error["family"] == "family"
    with pytest.raises(DescriptorValidationError) as exc2:
        await _target(world, sources=[world.a], exclude_signal_tags=[f"{FAMILY}:zzz"])
    assert exc2.value.validation_error["family"] == FAMILY
    assert exc2.value.validation_error["unknown_values"] == ["zzz"]
    with pytest.raises(DescriptorValidationError) as exc3:
        await world.reg.register(AnalystDescriptor.model_validate(_analyst_body(
            f"tax1_a_{uuid4().hex[:8]}", substrate={"signal_tags": ["demo.nope:x"]}),
            strict=False), "test")
    assert "demo.nope" in str(exc3.value)
    async with world.store.acquire() as conn:
        n = await conn.fetchval(
            "SELECT count(*) FROM target_descriptors WHERE descriptor_id LIKE 'tax1\\_t\\_%'")
    assert n == 0  # nothing landed


@pytest.mark.integration
async def test_the_declared_cap_holds_per_source_and_its_absence_changes_nothing(world: World):
    tid = await _target(world, sources=[world.a, world.b])
    capped = AnalystDescriptor.model_validate(_analyst_body(
        "tax1_capped", options={"slice_per_source_cap": 1}), strict=False)
    uncapped = AnalystDescriptor.model_validate(_analyst_body("tax1_uncapped", options={}),
                                                strict=False)
    got = await _unit_ids(world, tid, capped)
    # One of A's five and one of B's two — never two from one source.
    assert len(got) == 2 and sum(k.startswith("b") for k in got) == 1
    assert await _unit_ids(world, tid, uncapped) == set(world.ids)


@pytest.mark.integration
async def test_cross_target_or_semantics_attribution_and_cap(world: World):
    t1 = await _target(world, sources=[world.a], signal_tags=[f"{FAMILY}:x"])
    t2 = await _target(world, sources=[world.b], exclude_signal_tags=[f"{FAMILY}:x"])
    inv = {v: k for k, v in world.ids.items()}
    desc = SimpleNamespace(identity=SimpleNamespace(id="x", kind="cross_target_raw"),
                           subscription=SimpleNamespace(substrate={}, targets=None),
                           method=SimpleNamespace(options={}))
    async with world.store.acquire() as conn:
        rows = await READ_SLICE(conn, descriptor=desc, target_filter=None, target_ids=[t1, t2])
    got = {inv[str(r["id"])]: r["target_id"] for r in rows if str(r["id"]) in inv}
    # t1 keeps only A's tagged row; t2's exclude does not reach t1's rows.
    assert got == {"tagged": t1, "b2": t2}
    desc.subscription.substrate = {"signal_tags": [f"{FAMILY}:x"], "untagged_fallback": False}
    async with world.store.acquire() as conn:
        rows = await READ_SLICE(conn, descriptor=desc, target_filter=None, target_ids=[t1, t2])
    assert {inv[str(r["id"])] for r in rows if str(r["id"]) in inv} == {"tagged"}
    t3 = await _target(world, sources=[world.a, world.b])
    desc.subscription.substrate = {}
    desc.method.options = {"slice_per_source_cap": 1}
    async with world.store.acquire() as conn:
        rows = await READ_SLICE(conn, descriptor=desc, target_filter=None, target_ids=[t3])
    mine = [r for r in rows if str(r["id"]) in inv]
    assert len(mine) == 2 and {r["source_id"] for r in mine} == {world.a, world.b}


def test_signal_slice_scope_is_a_leaf_the_slim_registry_can_import():
    """Module level: stdlib + ``.tags`` + ``.subject_homonyms`` (B6 — itself a
    leaf over the gazetteer leaf) only (the option catalog is imported lazily
    inside ``slice_per_source_cap``) — registry/descriptor.py imports it."""
    import ast

    tree = ast.parse((_ROOT / "src/legba/data/signal_slice_scope.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert (node.level, node.module) in {(0, "__future__"), (0, "dataclasses"),
                                                 (0, "typing"), (1, "tags"),
                                                 (1, "subject_homonyms")}, node.module
        elif isinstance(node, ast.Import):
            # K-1: `re` for the text arm's term grammar — stdlib, still a leaf.
            assert [a.name for a in node.names] in (["logging"], ["re"])
