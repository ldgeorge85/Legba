# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for L-121 — QdrantVectorStoreHandler.

Per the task brief: real Qdrant container (already running per L-091). A
per-session unique collection prefix avoids collision with parallel agents.

The brief's required test surface:
  * upsert 100 vectors → search → top-k assertion
  * per-target collection create/delete round-trip
  * filter clauses (match / range / has_id)
  * healthcheck
"""

from __future__ import annotations

import math
import random
import uuid
from typing import Iterable

import pytest
import pytest_asyncio

from legba.data.stack.vector_store import (
    ConfigureContext,
    HandlerHealth,
    QdrantVectorStoreConfig,
    QdrantVectorStoreHandler,
    RuntimeContext,
    ScoredPoint,
    VectorPoint,
    collection_name_for_target,
)


# ---------------------------------------------------------------------------
# Session-scoped unique prefix so parallel agents don't collide.
# ---------------------------------------------------------------------------

SESSION_UUID = uuid.uuid4().hex[:10]
PREFIX = f"legba_test_{SESSION_UUID}_"


def _coll(name: str) -> str:
    return f"{PREFIX}{name}"


# Use a small dim to keep upserts fast.
TEST_DIM = 16


def _make_vector(seed: int, dim: int = TEST_DIM) -> list[float]:
    rng = random.Random(seed)
    raw = [rng.gauss(0.0, 1.0) for _ in range(dim)]
    # Normalise — search assertions rely on cosine ordering being stable.
    norm = math.sqrt(sum(x * x for x in raw)) or 1.0
    return [x / norm for x in raw]


# ---------------------------------------------------------------------------
# Fixture — handler that uses the env-driven config but with a per-session
# master collection so it can't trip over the live `legba_signals` data.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def handler() -> QdrantVectorStoreHandler:
    """A configured + activated handler. Master collection is a per-session
    name so the test never touches the production `legba_signals` data."""
    master = _coll("master")
    cfg = QdrantVectorStoreConfig(
        host="127.0.0.1",
        port=6333,
        default_dim=TEST_DIM,
        default_distance="Cosine",
        master_collection=master,
    )
    h = QdrantVectorStoreHandler(cfg)
    await h.on_configure(ConfigureContext(instance_id="test_instance"))
    await h.on_activate(RuntimeContext(instance_id="test_instance"))
    try:
        yield h
    finally:
        # Best-effort cleanup of any per-test collections that share our prefix.
        try:
            existing = await h.list_collections()
        except Exception:
            existing = []
        for name in existing:
            if name.startswith(PREFIX):
                try:
                    await h.delete_collection(name)
                except Exception:
                    pass
        await h.on_retire(RuntimeContext(instance_id="test_instance"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_handler_kind_metadata():
    """L-102 §1: every handler exposes kind / family / schema_version."""
    assert QdrantVectorStoreHandler.kind == "vector_store"
    assert QdrantVectorStoreHandler.family == "stack"
    assert QdrantVectorStoreHandler.schema_version.startswith("legba/stack/vector_store/")
    assert QdrantVectorStoreHandler.handler_version
    assert issubclass(QdrantVectorStoreHandler.config_schema, QdrantVectorStoreConfig.__mro__[1])  # BaseModel


@pytest.mark.integration
@pytest.mark.asyncio
async def test_target_collection_naming_convention():
    """Project-wide `legba_target__<target_id>` convention (matches L-001 + L-151)."""
    assert collection_name_for_target("brazil") == "legba_target__brazil"
    assert collection_name_for_target("ind-001") == "legba_target__ind-001"
    with pytest.raises(ValueError):
        collection_name_for_target("")
    with pytest.raises(ValueError):
        collection_name_for_target("has space")
    with pytest.raises(ValueError):
        collection_name_for_target("has/slash")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_on_configure_creates_master_collection(handler: QdrantVectorStoreHandler):
    """Brief item 6: `on_configure` verifies connectivity + master collection."""
    names = await handler.list_collections()
    assert handler.cfg.master_collection in names


@pytest.mark.integration
@pytest.mark.asyncio
async def test_healthcheck_reports_healthy(handler: QdrantVectorStoreHandler):
    """Brief item 5: healthcheck via `get_collections()`; non-empty list."""
    health: HandlerHealth = await handler.health_check(
        RuntimeContext(instance_id="test_instance")
    )
    assert health.state == "healthy"
    assert health.last_error is None
    assert health.last_success_at is not None
    assert handler.cfg.master_collection in health.detail["collections"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_search_top_k(handler: QdrantVectorStoreHandler):
    """Brief test 1: upsert 100 vectors, search, top-k assertion."""
    coll = _coll("upsert_search")
    created = await handler.create_collection(coll, dim=TEST_DIM, distance="Cosine")
    assert created is True

    # Re-create should be a no-op (idempotency).
    assert await handler.create_collection(coll, dim=TEST_DIM) is False

    # 100 normalised random vectors.
    points = [
        VectorPoint(
            id=i,
            vector=_make_vector(seed=i),
            payload={"i": i, "bucket": i % 5, "tag": "a" if i % 2 == 0 else "b"},
        )
        for i in range(100)
    ]
    n = await handler.upsert(coll, points, wait=True)
    assert n == 100

    # Search with the exact vector for id=7 — the top hit MUST be id=7.
    query_vec = points[7].vector
    hits = await handler.search(coll, query_vec, top_k=5)
    assert len(hits) == 5
    assert hits[0].id == 7
    assert math.isclose(hits[0].score, 1.0, rel_tol=0.0, abs_tol=1e-4)
    # Scores must be sorted descending (cosine higher == closer).
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    # Payload round-tripped.
    assert hits[0].payload["i"] == 7


@pytest.mark.integration
@pytest.mark.asyncio
async def test_per_target_collection_round_trip(handler: QdrantVectorStoreHandler):
    """Brief test 2: per-target create / delete round-trip."""
    target_id = f"t_{uuid.uuid4().hex[:8]}"
    name = await handler.ensure_target_collection(target_id, dim=TEST_DIM)
    assert name == collection_name_for_target(target_id)
    assert name.startswith("legba_target_")

    names = await handler.list_collections()
    assert name in names

    # Upsert a couple of points so the collection isn't empty.
    pts = [VectorPoint(id=1, vector=_make_vector(1)), VectorPoint(id=2, vector=_make_vector(2))]
    assert await handler.upsert(name, pts, wait=True) == 2

    # Delete via the per-target helper.
    dropped = await handler.delete_target_collection(target_id)
    assert dropped is True

    names = await handler.list_collections()
    assert name not in names

    # Re-delete is a no-op (returns False).
    assert await handler.delete_target_collection(target_id) is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_match_clause(handler: QdrantVectorStoreHandler):
    """Brief item 4: filter pass-through — match clause."""
    coll = _coll("filter_match")
    await handler.create_collection(coll, dim=TEST_DIM)

    points = [
        VectorPoint(
            id=i,
            vector=_make_vector(seed=i),
            payload={"category": "alpha" if i < 50 else "beta", "i": i},
        )
        for i in range(100)
    ]
    await handler.upsert(coll, points)

    # Match filter: only category=beta — top hit for id=80's vector should
    # still be 80 because it's in the matched set.
    flt = {
        "must": [
            {"key": "category", "match": {"value": "beta"}},
        ]
    }
    hits = await handler.search(coll, points[80].vector, top_k=5, filter=flt)
    assert len(hits) > 0
    assert hits[0].id == 80
    assert all(h.payload.get("category") == "beta" for h in hits)
    assert all(int(h.payload.get("i", 0)) >= 50 for h in hits)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_range_clause(handler: QdrantVectorStoreHandler):
    """Brief item 4: range clause via filter dict."""
    coll = _coll("filter_range")
    await handler.create_collection(coll, dim=TEST_DIM)

    points = [
        VectorPoint(
            id=i,
            vector=_make_vector(seed=i),
            payload={"score": i, "i": i},
        )
        for i in range(100)
    ]
    await handler.upsert(coll, points)

    # Range filter: 60 <= score < 70 — 10 candidates only.
    flt = {
        "must": [
            {"key": "score", "range": {"gte": 60, "lt": 70}},
        ]
    }
    # Query with a vector unrelated to those ids — confirm the filter clipped
    # the candidate set.
    hits = await handler.search(coll, points[65].vector, top_k=20, filter=flt)
    assert 1 <= len(hits) <= 10
    for h in hits:
        assert 60 <= int(h.payload["score"]) < 70


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_has_id_clause(handler: QdrantVectorStoreHandler):
    """Brief item 4: has_id clause."""
    coll = _coll("filter_has_id")
    await handler.create_collection(coll, dim=TEST_DIM)

    points = [VectorPoint(id=i, vector=_make_vector(seed=i)) for i in range(50)]
    await handler.upsert(coll, points)

    flt = {"must": [{"has_id": [3, 14, 27]}]}
    hits = await handler.search(coll, points[14].vector, top_k=10, filter=flt)
    returned = {h.id for h in hits}
    assert returned == {3, 14, 27}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_filter_must_not_clause(handler: QdrantVectorStoreHandler):
    """Compound: must_not against a tag — verifies dict→Filter normalisation
    covers more than just `must`."""
    coll = _coll("filter_must_not")
    await handler.create_collection(coll, dim=TEST_DIM)

    points = [
        VectorPoint(id=i, vector=_make_vector(seed=i),
                    payload={"tag": "x" if i % 2 == 0 else "y"})
        for i in range(40)
    ]
    await handler.upsert(coll, points)

    flt = {"must_not": [{"key": "tag", "match": {"value": "x"}}]}
    hits = await handler.search(coll, points[5].vector, top_k=10, filter=flt)
    assert all(h.payload.get("tag") == "y" for h in hits)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_by_ids(handler: QdrantVectorStoreHandler):
    """Operations surface: delete by id list."""
    coll = _coll("delete_ids")
    await handler.create_collection(coll, dim=TEST_DIM)

    points = [VectorPoint(id=i, vector=_make_vector(seed=i)) for i in range(20)]
    await handler.upsert(coll, points)

    n = await handler.delete(coll, [3, 5, 7])
    assert n == 3

    # has_id query for the deleted ids returns nothing.
    flt = {"must": [{"has_id": [3, 5, 7]}]}
    hits = await handler.search(coll, points[0].vector, top_k=10, filter=flt)
    assert hits == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifecycle_pause_and_resume_idempotent(handler: QdrantVectorStoreHandler):
    """on_pause flips the activated flag without dropping the connection;
    on_activate restores it."""
    rt = RuntimeContext(instance_id="test_instance")
    await handler.on_pause(rt)
    assert handler._activated is False
    # Operations still work (Phase-2 doesn't gate; runtime does, per L-160).
    names = await handler.list_collections()
    assert isinstance(names, list)
    await handler.on_activate(rt)
    assert handler._activated is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_on_activate_requires_master_collection():
    """If the master collection vanishes between configure and activate,
    activate fails fast."""
    master = _coll("vanishing_master")
    cfg = QdrantVectorStoreConfig(
        host="127.0.0.1", port=6333,
        default_dim=TEST_DIM, master_collection=master,
    )
    h = QdrantVectorStoreHandler(cfg)
    await h.on_configure(ConfigureContext(instance_id="vanishing"))
    try:
        # Operator drops the master collection out from under us.
        await h.store.client.delete_collection(collection_name=master)
        with pytest.raises(RuntimeError, match="missing"):
            await h.on_activate(RuntimeContext(instance_id="vanishing"))

        # Health is now degraded (collections list non-empty thanks to
        # other tests' collections, but master gone).
        health = await h.health_check(RuntimeContext(instance_id="vanishing"))
        assert health.state in {"degraded", "unhealthy"}
        assert health.detail.get("master_collection_present") is False
    finally:
        await h.on_retire(RuntimeContext(instance_id="vanishing"))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_on_retire_closes_store(handler: QdrantVectorStoreHandler):
    """on_retire releases the underlying QdrantStore."""
    # Use a fresh handler so we don't fight the fixture's teardown.
    master = _coll("retire_master")
    cfg = QdrantVectorStoreConfig(
        host="127.0.0.1", port=6333,
        default_dim=TEST_DIM, master_collection=master,
    )
    h = QdrantVectorStoreHandler(cfg)
    await h.on_configure(ConfigureContext(instance_id="retire"))
    rt = RuntimeContext(instance_id="retire")
    await h.on_activate(rt)
    await h.on_retire(rt)
    # After retire, `store` raises.
    with pytest.raises(RuntimeError, match="not configured"):
        _ = h.store
    # health_check returns unhealthy
    health = await h.health_check(rt)
    assert health.state == "unhealthy"
    # Cleanup the master collection so we don't leak.
    cleanup = QdrantVectorStoreHandler(cfg)
    await cleanup.on_configure(ConfigureContext(instance_id="cleanup"))
    try:
        await cleanup.delete_collection(master)
    finally:
        await cleanup.on_retire(rt)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_search_empty_collection_returns_empty(handler: QdrantVectorStoreHandler):
    coll = _coll("empty")
    await handler.create_collection(coll, dim=TEST_DIM)
    hits = await handler.search(coll, _make_vector(0), top_k=5)
    assert hits == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_zero_points_is_noop(handler: QdrantVectorStoreHandler):
    coll = _coll("noop_upsert")
    await handler.create_collection(coll, dim=TEST_DIM)
    assert await handler.upsert(coll, []) == 0
    assert await handler.delete(coll, []) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_config_from_legba_data_qdrant_config():
    """from_env path: builds handler config from L-001 bootstrap env config."""
    from legba.data.config import QdrantConfig
    cfg = QdrantVectorStoreConfig.from_legba_data_qdrant_config(QdrantConfig.from_env())
    # Defaults survive the conversion.
    assert cfg.default_dim == 1024
    assert cfg.default_distance == "Cosine"
    assert cfg.master_collection  # signals_collection from env
