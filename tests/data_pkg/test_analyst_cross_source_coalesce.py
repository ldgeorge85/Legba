# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ``cross_source_coalesce`` deterministic sub-handler (P2).

Three layers:

  * **Pure pairing core** — the cross-source semantic/temporal coalesce logic
    over pre-computed vectors (no embedding/Qdrant backend needed): two sources
    reporting the SAME event with close titles + in-window → coalesced; the
    cross-source-only / temporal-window / title-distance guards each block a
    false link. Runs in every CI lane.
  * **Synthetic embed path** (``deps=None`` + an injected fake embedder
    satisfying the L-122 :class:`EmbeddingService` port) — proves the handler
    embeds then coalesces end-to-end without a real model/vector store, AND the
    off-by-default + degrade-not-drop (SEAM #19) refusals.
  * **Live pivot DB** (env-gated) — the P2 acceptance: insert the SAME event via
    2 ``source_id``s into ``legba_pivot_test``, run the handler with a fake
    embedder + fake Qdrant, and assert 1 canonical + 1 alias with BOTH raw rows
    preserved + idempotent rerun. Skips cleanly when the dev rig is down.

The dispatcher contract (registered in
:data:`legba.data.analysts.deterministic.SUB_HANDLERS`) is asserted too.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    run_method,
)
from legba.data.analysts.deterministic_handlers import cross_source_coalesce
from legba.data.provenance.models import FindingPayload
from legba.runtime.analyst_method import AnalystMethodResult

SUB = "cross_source_coalesce"

# A small fixed pair of nearly-orthogonal unit-ish vectors. ``_VEC_EVENT`` and a
# tiny-perturbation of it are ~1.0 cosine (same event); ``_VEC_OTHER`` is
# near-orthogonal (~0 cosine, a different event).
_VEC_EVENT = [1.0, 0.0, 0.0, 0.0]
_VEC_EVENT_NEAR = [0.999, 0.04, 0.0, 0.0]   # cosine ~0.9992 vs _VEC_EVENT
_VEC_OTHER = [0.0, 0.0, 1.0, 0.0]           # cosine 0.0 vs _VEC_EVENT


class _FakeEmbedder:
    """Process-local L-122 :class:`EmbeddingService` port.

    Maps a substring of the embed text to a canned vector so the handler's
    embed→coalesce path is exercised deterministically without a real model.
    """

    dim = 4

    def __init__(self, mapping: dict[str, list[float]], default: list[float] | None = None):
        self._mapping = mapping
        self._default = default or _VEC_OTHER

    async def embed(self, text: str) -> list[float]:
        for needle, vec in self._mapping.items():
            if needle in text:
                return list(vec)
        return list(self._default)


class _FakeExtrasDeps:
    """Minimal deps carrier: only ``extras`` (no pg_pool) for the embed path."""

    def __init__(self, extras: dict[str, Any]):
        self.extras = extras
        self.pg_pool = None


def _row(sid: str, source: str, title: str, *, when: datetime, vec: list[float] | None = None):
    r: dict[str, Any] = {
        "id": sid,
        "source_id": source,
        "fetched_at": when,
        "payload": {"title": title, "summary": title},
    }
    if vec is not None:
        r["_vector"] = vec
    return r


_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_cross_source_coalesce_registered():
    assert SUB in SUB_HANDLERS, "cross_source_coalesce missing from SUB_HANDLERS"
    assert SUB in OUTPUT_KIND_BY_SUB_HANDLER
    assert SUB_HANDLERS[SUB] is cross_source_coalesce.handle


# ---------------------------------------------------------------------------
# Pure pairing core — coalesce over pre-computed vectors
# ---------------------------------------------------------------------------


async def test_two_sources_same_event_coalesced():
    sig_a, sig_b = str(uuid4()), str(uuid4())
    inputs = [
        # SAME event, 2 sources, close titles, 5 min apart, near-identical vector
        _row(sig_a, "src_reuters", "Quake hits coastal region", when=_T0, vec=_VEC_EVENT),
        _row(sig_b, "src_ap", "Quake hits coastal region today",
             when=_T0 + timedelta(minutes=5), vec=_VEC_EVENT_NEAR),
    ]
    result = await run_method(
        inputs,
        {"sub_handler": SUB, "enabled": True, "analyst_id": "coalesce", "run_id": uuid4()},
        None,
    )
    assert isinstance(result, AnalystMethodResult)
    assert isinstance(result.finding, FindingPayload)
    data = result.finding.data
    assert data["sub_handler"] == SUB
    assert data["coalesced_sets"] == 1, data
    assert data["aliases_linked"] == 1, data
    one = data["sets"][0]
    # deterministic canonical = earliest fetched_at = sig_a
    assert one["canonical_signal_id"] == sig_a
    assert one["alias_signal_ids"] == [sig_b]
    assert one["reason"] == "cross_source_coalesce"
    assert one["score"] > 0.92
    # never spends tokens
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }


async def test_same_source_near_dup_not_linked():
    # Two near-identical signals from the SAME source — the ingest-time filter's
    # job, NOT this cross-source coalescer's. Must NOT link.
    inputs = [
        _row(str(uuid4()), "src_reuters", "Quake hits region", when=_T0, vec=_VEC_EVENT),
        _row(str(uuid4()), "src_reuters", "Quake hits region",
             when=_T0 + timedelta(minutes=2), vec=_VEC_EVENT_NEAR),
    ]
    result = await run_method(inputs, {"sub_handler": SUB, "enabled": True}, None)
    assert result.finding.data["coalesced_sets"] == 0
    assert result.finding.data["aliases_linked"] == 0


async def test_out_of_window_not_linked():
    # Same event, 2 sources, near-identical vector + title — but 48h apart,
    # outside the default 24h window. The tier-4 temporal guard blocks the link.
    inputs = [
        _row(str(uuid4()), "src_a", "Quake hits region", when=_T0, vec=_VEC_EVENT),
        _row(str(uuid4()), "src_b", "Quake hits region",
             when=_T0 + timedelta(hours=48), vec=_VEC_EVENT_NEAR),
    ]
    result = await run_method(inputs, {"sub_handler": SUB, "enabled": True}, None)
    assert result.finding.data["coalesced_sets"] == 0


async def test_different_event_not_linked():
    # 2 sources, in window, but ORTHOGONAL vectors (different events) — the tier-3
    # cosine floor blocks the link even though both within the window.
    inputs = [
        _row(str(uuid4()), "src_a", "Quake hits region", when=_T0, vec=_VEC_EVENT),
        _row(str(uuid4()), "src_b", "Central bank raises rates",
             when=_T0 + timedelta(minutes=10), vec=_VEC_OTHER),
    ]
    result = await run_method(inputs, {"sub_handler": SUB, "enabled": True}, None)
    assert result.finding.data["coalesced_sets"] == 0


async def test_high_cosine_but_distant_titles_not_linked():
    # High cosine + in window, but the titles are far apart (tier-4 title guard
    # blocks it — a vector collision shouldn't over-link unrelated headlines).
    inputs = [
        _row(str(uuid4()), "src_a", "Quake hits coastal region", when=_T0, vec=_VEC_EVENT),
        _row(str(uuid4()), "src_b", "Completely unrelated banking headline here",
             when=_T0 + timedelta(minutes=5), vec=_VEC_EVENT_NEAR),
    ]
    result = await run_method(inputs, {"sub_handler": SUB, "enabled": True}, None)
    assert result.finding.data["coalesced_sets"] == 0


async def test_three_sources_same_event_one_canonical():
    # 3 sources, same event → one connected component, one canonical, 2 aliases.
    sig_a, sig_b, sig_c = str(uuid4()), str(uuid4()), str(uuid4())
    inputs = [
        _row(sig_a, "src_a", "Quake hits region", when=_T0, vec=_VEC_EVENT),
        _row(sig_b, "src_b", "Quake hits region now",
             when=_T0 + timedelta(minutes=3), vec=_VEC_EVENT_NEAR),
        _row(sig_c, "src_c", "Quake hits region update",
             when=_T0 + timedelta(minutes=6), vec=_VEC_EVENT),
    ]
    result = await run_method(inputs, {"sub_handler": SUB, "enabled": True}, None)
    data = result.finding.data
    assert data["coalesced_sets"] == 1, data
    assert data["aliases_linked"] == 2, data
    one = data["sets"][0]
    assert one["canonical_signal_id"] == sig_a
    assert sorted(one["alias_signal_ids"]) == sorted([sig_b, sig_c])


# ---------------------------------------------------------------------------
# Off-by-default + degrade-not-drop (SEAM #19)
# ---------------------------------------------------------------------------


async def test_disabled_by_default_no_op():
    # enabled defaults False — even with a perfect coalesce pair, no-op.
    inputs = [
        _row(str(uuid4()), "src_a", "Quake hits region", when=_T0, vec=_VEC_EVENT),
        _row(str(uuid4()), "src_b", "Quake hits region",
             when=_T0 + timedelta(minutes=5), vec=_VEC_EVENT_NEAR),
    ]
    result = await run_method(inputs, {"sub_handler": SUB}, None)  # no enabled key
    assert result.finding.data["coalesced_sets"] == 0
    assert result.finding.data["aliases_linked"] == 0
    assert result.finding.data.get("disabled") is True


async def test_synthetic_embed_path_with_fake_embedder():
    # No precomputed _vector; an injected EmbeddingService port embeds the rows.
    embedder = _FakeEmbedder({"Quake": _VEC_EVENT}, default=_VEC_OTHER)
    sig_a, sig_b = str(uuid4()), str(uuid4())
    inputs = [
        _row(sig_a, "src_a", "Quake hits region", when=_T0),
        _row(sig_b, "src_b", "Quake hits region today",
             when=_T0 + timedelta(minutes=4)),
    ]
    result = await run_method(
        inputs,
        {"sub_handler": SUB, "enabled": True, "_test_embedder": embedder},
        None,
    )
    data = result.finding.data
    assert data["embedded"] == 2, data
    assert data["coalesced_sets"] == 1, data
    assert data["sets"][0]["canonical_signal_id"] == sig_a


async def test_no_vectors_no_embedder_unavailable():
    # deps=None, rows lack _vector, no _test_embedder → cannot coalesce. Must
    # refuse loud (unavailable), NOT fabricate a link.
    inputs = [
        _row(str(uuid4()), "src_a", "Quake hits region", when=_T0),
        _row(str(uuid4()), "src_b", "Quake hits region", when=_T0),
    ]
    result = await run_method(inputs, {"sub_handler": SUB, "enabled": True}, None)
    data = result.finding.data
    assert data["coalesced_sets"] == 0
    assert data.get("unavailable")
    assert "coalesce_unavailable" in result.finding.tags


async def test_live_path_missing_ports_refuses_loud():
    # The live-pool path (pg_pool present) with NO embedding_service / qdrant in
    # extras → SEAM #19 refusal: unavailable finding, zero links. Uses a deps
    # whose pg_pool is a sentinel object (never queried — the missing-ports guard
    # returns BEFORE any DB access).
    class _SentinelPool:
        pass

    deps = _FakeExtrasDeps(extras={})
    deps.pg_pool = _SentinelPool()  # type: ignore[assignment]
    result = await run_method([], {"sub_handler": SUB, "enabled": True}, deps)
    data = result.finding.data
    assert data["coalesced_sets"] == 0
    assert str(data.get("unavailable", "")).startswith("missing:")
    assert "embedding_service" in data["unavailable"]
    assert "qdrant" in data["unavailable"]
    assert "coalesce_unavailable" in result.finding.tags


# ---------------------------------------------------------------------------
# Live pivot-DB acceptance (env-gated)
# ---------------------------------------------------------------------------


_PIVOT_DB = {
    "host": os.environ.get("LEGBA_PIVOT_PG_HOST", "127.0.0.1"),
    "port": int(os.environ.get("LEGBA_PIVOT_PG_PORT", "5432")),
    "user": os.environ.get("LEGBA_PIVOT_PG_USER", "legba"),
    "password": os.environ.get("LEGBA_PIVOT_PG_PASSWORD", "legba"),
    "database": os.environ.get("LEGBA_PIVOT_PG_DB", "legba_pivot_test"),
}


class _FakeQdrant:
    """Process-local async Qdrant stand-in — accepts collection mgmt + upsert.

    The handler's coalesce pairing runs over the in-memory vectors; Qdrant is
    only the (best-effort) shared-collection persist. This fake records upserts
    so the test can confirm the persist path was exercised without a real
    cluster.
    """

    def __init__(self):
        self.collections: set[str] = set()
        self.upserted: list[Any] = []

    async def get_collections(self):
        class _C:
            def __init__(self, name): self.name = name
        class _R:
            def __init__(self, names): self.collections = [_C(n) for n in names]
        return _R(self.collections)

    async def create_collection(self, *, collection_name, vectors_config):  # noqa: ANN001
        self.collections.add(collection_name)

    async def upsert(self, *, collection_name, points):  # noqa: ANN001
        self.upserted.extend(points)


@pytest.fixture
async def pivot_pool():
    asyncpg = pytest.importorskip("asyncpg")
    try:
        pool = await asyncpg.create_pool(min_size=1, max_size=4, **_PIVOT_DB)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"legba_pivot_test unreachable: {exc}")
    async with pool.acquire() as conn:
        ok = await conn.fetchval("SELECT to_regclass('signal_aliases')")
        has_canon = await conn.fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name='signals' AND column_name='canonical_signal_id'"
        )
    if not ok or not has_canon:
        await pool.close()
        pytest.skip("pivot substrate (signal_aliases / canonical_signal_id) not present")
    yield pool
    await pool.close()


async def test_live_pivot_acceptance(pivot_pool):
    """P2 acceptance — same event via 2 sources (different wording, no shared
    content_hash) => 1 canonical + 1 cross_source_coalesce alias, both raw rows
    preserved, canonical_only sees 1, rerun idempotent."""
    import json

    from legba.runtime.deps import StandardDeps

    tenant = f"coalesce_test_{uuid4().hex[:8]}"
    produced_by = f"test_coalesce_{uuid4().hex[:8]}"
    sig_a, sig_b = uuid4(), uuid4()

    # DIFFERENT wording + DIFFERENT content_hash — the cross_source_dedup exact
    # path would NOT link these; only the semantic coalesce does. fetched_at is
    # set relative to NOW() (sig_a is 2h ago, sig_b 116min ago — both inside the
    # default 24h window; sig_a is earliest → the deterministic canonical).
    async with pivot_pool.acquire() as conn:
        for sid, src, age_min, payload, ch in [
            (sig_a, "source_reuters", 120,
             {"title": "Major earthquake strikes coastal region"}, f"h_{uuid4().hex}"),
            (sig_b, "source_ap", 116,
             {"title": "Major earthquake strikes coastal region overnight"}, f"h_{uuid4().hex}"),
        ]:
            await conn.execute(
                """INSERT INTO signals
                       (id, source_id, owner_tenant, modality, payload, content_hash, fetched_at)
                   VALUES ($1,$2,$3,'text',$4::jsonb,$5, NOW() - ($6 || ' minutes')::interval)""",
                sid, src, tenant, json.dumps(payload), ch, str(age_min),
            )

    embedder = _FakeEmbedder({"earthquake": _VEC_EVENT})  # both titles → same vec
    # Perturb the second so cosine is ~1.0 but not exactly identical-id collisions.
    qdrant = _FakeQdrant()
    deps = StandardDeps(
        pg_pool=pivot_pool,
        extras={"embedding_service": embedder, "qdrant": qdrant},
    )
    opts = {
        "sub_handler": SUB, "enabled": True, "analyst_id": produced_by,
        "run_id": uuid4(), "owner_tenant": tenant, "window_hours": 24,
    }
    try:
        result = await run_method([], opts, deps)
        data = result.finding.data
        assert data["coalesced_sets"] == 1, data
        assert data["aliases_linked"] == 1, data
        assert data["embedded"] == 2, data
        # The shared-collection persist path ran.
        assert len(qdrant.upserted) == 2
        assert "legba_coalesce" in qdrant.collections

        async with pivot_pool.acquire() as conn:
            # BOTH raw rows survive — link, never collapse.
            raw = await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant)
            assert raw == 2

            aliases = await conn.fetch(
                "SELECT alias_signal_id, canonical_signal_id, reason "
                "FROM signal_aliases WHERE produced_by=$1", produced_by)
            assert len(aliases) == 1
            assert str(aliases[0]["canonical_signal_id"]) == str(sig_a)  # earliest
            assert str(aliases[0]["alias_signal_id"]) == str(sig_b)
            assert aliases[0]["reason"] == "cross_source_coalesce"

            # canonical points at itself; alias points at canonical.
            ca = await conn.fetchval(
                "SELECT canonical_signal_id FROM signals WHERE id=$1", sig_a)
            cb = await conn.fetchval(
                "SELECT canonical_signal_id FROM signals WHERE id=$1", sig_b)
            assert str(ca) == str(sig_a)
            assert str(cb) == str(sig_a)

            # a canonical_only subscription sees exactly 1.
            canon_only = await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1 "
                "AND (canonical_signal_id = id OR canonical_signal_id IS NULL)", tenant)
            assert canon_only == 1

        # Rerun idempotent — links 0 NEW aliases (the alias is no longer in the
        # un-aliased window), never collapses.
        rerun = await run_method([], opts, deps)
        assert rerun.finding.data["aliases_linked"] == 0
        async with pivot_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM signal_aliases WHERE produced_by=$1",
                produced_by) == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM signals WHERE owner_tenant=$1", tenant) == 2
    finally:
        async with pivot_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM signal_aliases WHERE produced_by=$1", produced_by)
            await conn.execute(
                "DELETE FROM signals WHERE owner_tenant=$1", tenant)
