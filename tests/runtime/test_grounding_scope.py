# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OPEN-1 (W2) — the world-context retrieval scope is EXPLICIT.

Before: any target id that did not parse as ``country_<tier>_<iso2>`` got NO
Qdrant filter — the whole curated collection. After:

  * a per-country desk → its ISO-2 filter (unchanged);
  * a no-target run, or a target whose KIND is declared global (scope tag
    ``region``/``lane``/``flow``/``situation``/``world``; the id family is the
    fallback when the head is unreadable) → no filter, as before, but logged
    ``grounding.scope=global reason=<kind>``;
  * ANY other target → no retrieval at all: an empty, typed result carrying
    ``reason=unresolvable_scope``. The embedder and Qdrant are never touched.

BYTE-IDENTITY. The exemplar roster is rebuilt from the repository's own
registration sources (the country bring-up tables, the region registrar, the
committed lane/flow/situation descriptors) — every non-retired target the
exemplar registers, a superset of the live active heads — and every id must
resolve exactly as the legacy rule did, on BOTH the scope-tag path and the
id-family fallback.
"""

from __future__ import annotations

import logging
import pathlib
import sys
from typing import Any

import pytest
import yaml

from legba.runtime.grounding import (
    SubstrateGroundingResolver,
    world_context_country_filter_values,
)
from legba.runtime.grounding_scope import (
    GLOBAL_SCOPE_KINDS,
    REASON_UNRESOLVABLE,
    SCOPE_COUNTRY,
    SCOPE_GLOBAL,
    SCOPE_NONE,
    WorldContextChunks,
    classify_world_context_scope,
    declared_global_kind,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import bringup_register_region_targets as region_reg  # noqa: E402
import bringup_tag_targets as country_tags  # noqa: E402


# ---------------------------------------------------------------------------
# The exemplar roster, from the repository's own registration sources
# ---------------------------------------------------------------------------


def _country_roster() -> dict[str, list[str]]:
    # ROSTER-2: the desk list is the profile's (bringup_tag_targets.desks());
    # the tier is the desk id's registrar convention, country_<tier>_<iso2>.
    out: dict[str, list[str]] = {}
    for desc_id, iso in country_tags.desks():
        tier = desc_id.split("_")[1]
        tags = ["geopolitical", "news", tier, country_tags.REGION_BY_ISO2[iso]]
        tags += country_tags.WATCH_TAGS_BY_ISO2.get(iso, [])
        out[desc_id] = tags
    return out


def _region_roster() -> dict[str, list[str]]:
    return {
        desc_id: list(body["scope"]["tags"])
        for desc_id, body, _desc in region_reg.build_region_frames()
    }


def _committed_thematic_roster() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for prefix in ("lane", "flow", "situation"):
        for path in sorted((REPO_ROOT / "descriptors").glob(f"target_{prefix}_*.yaml")):
            body = yaml.safe_load(path.read_text())
            if body["identity"].get("state") == "retired":
                continue
            out[body["identity"]["id"]] = list(body["scope"].get("tags") or [])
    return out


def _exemplar_roster() -> dict[str, list[str]]:
    roster: dict[str, list[str]] = {}
    roster.update(_country_roster())
    roster.update(_region_roster())
    roster.update(_committed_thematic_roster())
    return roster


def _legacy_outcome(target_id: str | None) -> tuple[str, ...] | None:
    """What the pre-OPEN-1 resolver did: the ISO filter, or None = the whole
    collection (no filter) for EVERY other target."""
    values = world_context_country_filter_values(target_id)
    return tuple(values) if values else None


def test_exemplar_roster_is_the_shape_the_live_registry_carries():
    roster = _exemplar_roster()
    countries = [t for t in roster if t.startswith("country_")]
    regions = [t for t in roster if t.startswith("region_")]
    lanes = [t for t in roster if t.startswith("lane_")]
    flows = [t for t in roster if t.startswith("flow_")]
    situations = [t for t in roster if t.startswith("situation_")]
    assert (len(countries), len(regions), len(lanes), len(flows), len(situations)) == (
        32, 5, 6, 4, 1,
    )
    # Every live-active head on 2026-10-03 (44: 32 country, 5 region, 4 lane,
    # 2 flow, 1 situation) is a member of this 48-id roster.
    assert len(roster) == 48


@pytest.mark.parametrize("use_tags", [True, False], ids=["scope_tag", "id_prefix"])
def test_every_exemplar_target_resolves_exactly_as_before(use_tags):
    """BYTE-IDENTITY: per id, the filter (or its absence) is unchanged, and no
    exemplar target loses retrieval."""
    for target_id, tags in sorted(_exemplar_roster().items()):
        legacy = _legacy_outcome(target_id)
        scope = classify_world_context_scope(
            target_id,
            world_context_country_filter_values(target_id),
            tags if use_tags else None,
        )
        assert scope.retrieves, target_id
        assert scope.country_values == legacy, target_id
        if legacy is None:
            assert scope.scope == SCOPE_GLOBAL, target_id
            assert scope.reason == target_id.split("_", 1)[0], target_id
        else:
            assert scope.scope == SCOPE_COUNTRY, target_id


def test_no_target_meta_run_stays_global():
    scope = classify_world_context_scope(None, None)
    assert scope.scope == SCOPE_GLOBAL and scope.reason == "no_target"
    assert scope.country_values is None and scope.retrieves


@pytest.mark.parametrize(
    "target_id",
    [
        "germany_news",          # committed example target: a desk, not a kind
        "acme_supplier_desk",    # a non-geopolitical instance's target
        "thematic_energy",       # a family nobody declared global
        "country_g20_xyz",       # a country-shaped id with no ISO-2
        "regional_desk",         # NOT the region family (no ``region_``)
    ],
)
def test_any_other_target_gets_no_retrieval(target_id):
    scope = classify_world_context_scope(
        target_id, world_context_country_filter_values(target_id), None,
    )
    assert scope.scope == SCOPE_NONE
    assert scope.reason == REASON_UNRESOLVABLE
    assert not scope.retrieves


def test_scope_tags_are_authoritative_when_the_head_answers():
    # A lane-named target whose head declares no global kind is NOT global.
    assert declared_global_kind("lane_x", ["supply_chain"]) is None
    # A target whose head DOES declare one is global whatever its id.
    assert declared_global_kind("acme_corridor", ["supply_chain", "lane"]) == (
        "lane", "scope_tag",
    )
    # A country desk's coverage tag is not the region kind.
    assert declared_global_kind("x", ["region_europe"]) is None
    # Fallback only when the head is unreadable.
    assert declared_global_kind("region_europe", None) == ("region", "id_prefix")
    assert declared_global_kind("world", None) == ("world", "id_prefix")
    assert set(GLOBAL_SCOPE_KINDS) == {"region", "lane", "flow", "situation", "world"}


# ---------------------------------------------------------------------------
# Through the resolver (the real binding path the deps-builder hook calls)
# ---------------------------------------------------------------------------


class _Point:
    def __init__(self, id: str, countries: list[str]) -> None:
        self.id = id
        self.payload = {"corpus": "world_context", "title": id, "text": "a prior",
                        "countries": countries}
        self.score = 0.8


class _Resp:
    def __init__(self, points: list[_Point]) -> None:
        self.points = points


class _Qdrant:
    def __init__(self, points: list[_Point]) -> None:
        self._points = points
        self.calls: list[dict[str, Any]] = []

    async def query_points(self, **kwargs: Any) -> _Resp:
        self.calls.append(kwargs)
        return _Resp(list(self._points))


class _Embedder:
    dim = 4

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> list[float]:  # noqa: ARG002
        self.calls += 1
        return [0.1, 0.2, 0.3, 0.4]


class _TagConn:
    def __init__(self, heads: dict[str, Any], log: list[tuple[str, tuple]], fail: bool):
        self._heads = heads
        self._log = log
        self._fail = fail

    async def fetchval(self, sql: str, *params: Any) -> Any:
        self._log.append((sql, params))
        if self._fail:
            raise RuntimeError("registry unreachable")
        return self._heads.get(params[0])


class _Acquire:
    def __init__(self, conn: _TagConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _TagConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _TagPool:
    """Answers the target-head scope-tag read the way asyncpg does without a
    jsonb codec (the tags come back as JSON text)."""

    def __init__(self, heads: dict[str, Any] | None = None, *, fail: bool = False) -> None:
        self.log: list[tuple[str, tuple]] = []
        self._conn = _TagConn(heads or {}, self.log, fail)

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


def _resolver(pool: Any) -> tuple[SubstrateGroundingResolver, _Qdrant, _Embedder]:
    qdrant = _Qdrant([_Point("fr-1", ["fr", "FR"]), _Point("ir-1", ["ir", "IR"])])
    emb = _Embedder()
    return SubstrateGroundingResolver(pg_pool=pool, embedder=emb, qdrant=qdrant), qdrant, emb


@pytest.mark.asyncio
async def test_declared_lane_reads_unfiltered_and_logs_global(caplog):
    pool = _TagPool({"lane_hormuz": '["supply_chain", "disruption", "lane"]'})
    resolver, qdrant, _ = _resolver(pool)
    with caplog.at_level(logging.INFO, logger="legba.runtime.grounding_scope"):
        chunks = await resolver.resolve_world_context("q", limit=10, target_id="lane_hormuz")
    assert {c.chunk_id for c in chunks} == {"fr-1", "ir-1"}
    assert qdrant.calls[-1]["query_filter"] is None
    assert isinstance(chunks, WorldContextChunks) and chunks.reason == "lane"
    assert "grounding.scope=global reason=lane source=scope_tag" in caplog.text


@pytest.mark.asyncio
async def test_unresolvable_target_never_touches_the_collection(caplog):
    """THE FAIL-OPEN, CLOSED: a target that is neither a country desk nor a
    declared-global kind gets an EMPTY typed result — no embed, no search."""
    pool = _TagPool({"acme_supplier_desk": '["supply_chain", "corporate"]'})
    resolver, qdrant, emb = _resolver(pool)
    with caplog.at_level(logging.WARNING, logger="legba.runtime.grounding_scope"):
        chunks = await resolver.resolve_world_context(
            "q", limit=10, target_id="acme_supplier_desk",
        )
    assert chunks == []
    assert isinstance(chunks, WorldContextChunks)
    assert chunks.reason == REASON_UNRESOLVABLE
    assert qdrant.calls == [] and emb.calls == 0
    assert "grounding.scope=none reason=unresolvable_scope" in caplog.text


@pytest.mark.asyncio
async def test_unregistered_non_family_target_gets_no_retrieval():
    resolver, qdrant, _ = _resolver(_TagPool({}))
    chunks = await resolver.resolve_world_context("q", limit=10, target_id="germany_news")
    assert chunks == [] and chunks.reason == REASON_UNRESOLVABLE
    assert qdrant.calls == []


@pytest.mark.asyncio
async def test_registry_failure_falls_back_to_the_id_family():
    pool = _TagPool(fail=True)
    resolver, qdrant, _ = _resolver(pool)
    chunks = await resolver.resolve_world_context("q", limit=10, target_id="region_europe")
    assert len(chunks) == 2 and chunks.reason == "region"
    assert qdrant.calls[-1]["query_filter"] is None
    # ...and a non-family id still gets nothing when the registry is down.
    none = await resolver.resolve_world_context("q", limit=10, target_id="acme_x")
    assert none == [] and none.reason == REASON_UNRESOLVABLE


@pytest.mark.asyncio
async def test_country_desk_filters_and_never_reads_the_head():
    pool = _TagPool({})
    resolver, qdrant, _ = _resolver(pool)
    chunks = await resolver.resolve_world_context("q", limit=10, target_id="country_g20_fr")
    assert list(qdrant.calls[-1]["query_filter"].must[0].match.any) == ["fr", "FR"]
    assert chunks.scope.scope == SCOPE_COUNTRY
    assert pool.log == []


@pytest.mark.asyncio
async def test_scope_tags_are_cached_per_resolver():
    pool = _TagPool({"flow_semiconductors": '["supply_chain", "disruption", "flow"]'})
    resolver, _, _ = _resolver(pool)
    for _ in range(3):
        await resolver.resolve_world_context("q", limit=10, target_id="flow_semiconductors")
    assert len(pool.log) == 1
