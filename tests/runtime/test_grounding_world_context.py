# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S5-T3 opportunistic RAG — the ``vector:world_context`` grounding source.

Infra-free unit tests (no live Postgres / Qdrant / embedding endpoint) for the
inline ASSEMBLE-time RAG that adds a SEPARATE, non-citable "BACKGROUND PRIORS
(context, not evidence — do not cite)" block BELOW the authoritative preamble:

  * ``SubstrateGroundingResolver.resolve_world_context`` embeds the query and
    cosine-searches the ``world_context`` collection, mapping hits → chunks;
  * degrade-not-drop / honesty: no embedder, no qdrant, empty query, an EMPTY
    collection, an embed failure, or a search failure all yield NO chunks (→ no
    block, no fabricated header);
  * ``build_world_context_block`` renders the EXACT non-citable header + is
    token-capped, and returns ``None`` when empty;
  * the deps-builder hook, wired with ``sources=[substrate, vector:world_context]``,
    emits the BACKGROUND PRIORS block BELOW the AUTHORITATIVE preamble AND records
    the retrieved chunk ids into the caller's trace sink;
  * end-to-end through ``inline_target.run_method``: the ``inject_preamble`` trace
    event carries ``world_context_chunk_ids`` for auditable retrieval.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4

import pytest

from legba.data.analysts.inline_target import (
    GROUNDING_RAG_CHUNK_SINK_KEY,
    InlineTargetDeps,
    run_method,
)
from legba.runtime.analyst_deps_builder import _build_grounding_hook
from legba.runtime.grounding import (
    GroundingWorldContextChunk,
    SubstrateGroundingResolver,
    build_world_context_block,
    world_context_country_filter_values,
    world_context_min_score,
)


# ---------------------------------------------------------------------------
# In-process fakes
# ---------------------------------------------------------------------------


class _FakePoint:
    """A single scored Qdrant hit (mirrors ``QueryResponse.points`` item)."""

    def __init__(self, *, id: str, payload: dict[str, Any], score: float) -> None:
        self.id = id
        self.payload = payload
        self.score = score


class _FakeQueryResponse:
    def __init__(self, points: list[_FakePoint]) -> None:
        self.points = points


class _FakeQdrant:
    """Async Qdrant stand-in exposing ``query_points``; records the last call."""

    def __init__(self, points: list[_FakePoint]) -> None:
        self._points = points
        self.last_call: dict[str, Any] | None = None

    async def query_points(self, **kwargs: Any) -> _FakeQueryResponse:
        self.last_call = kwargs
        return _FakeQueryResponse(list(self._points))


class _RaisingQdrant:
    async def query_points(self, **kwargs: Any) -> Any:
        raise RuntimeError("qdrant unreachable")


class _StubEmbedder:
    """Records the embedded text; returns a fixed vector."""

    dim = 4

    def __init__(self, vector: list[float] | None = None) -> None:
        self._vector = vector or [0.1, 0.2, 0.3, 0.4]
        self.embedded: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.embedded.append(text)
        return list(self._vector)


class _RaisingEmbedder:
    dim = 4

    async def embed(self, text: str) -> list[float]:  # noqa: ARG002
        raise RuntimeError("embedding backend unreachable")


def _chunk_point(
    *, id: str = "chunk-1", text: str = "A prior on succession dynamics.",
    title: str = "Succession primer", section: str = "Overview",
    source_url: str = "https://example.invalid/doc", score: float = 0.87,
) -> _FakePoint:
    return _FakePoint(
        id=id,
        payload={
            "corpus": "world_context",
            "doc_id": "doc-1",
            "title": title,
            "section": section,
            "source_url": source_url,
            "text": text,
        },
        score=score,
    )


# --- Postgres stub (facts/nexuses/situations by SQL keyword) ----------------


class _StubConn:
    def __init__(self, fetch_rows: dict[str, list[dict[str, Any]]], log: list[tuple[str, tuple]]):
        self._fetch_rows = fetch_rows
        self._log = log

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self._log.append((sql, params))
        if "FROM facts" in sql:
            return self._fetch_rows.get("facts", [])
        if "FROM nexuses" in sql:
            return self._fetch_rows.get("nexuses", [])
        if "FROM situations" in sql:
            return self._fetch_rows.get("situations", [])
        if "FROM graph_metrics" in sql:
            return self._fetch_rows.get("graph_metrics", [])
        return []


class _StubAcquire:
    def __init__(self, conn: _StubConn):
        self._conn = conn

    async def __aenter__(self) -> _StubConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _StubPool:
    def __init__(self, fetch_rows: dict[str, list[dict[str, Any]]] | None = None):
        self.log: list[tuple[str, tuple]] = []
        self._conn = _StubConn(fetch_rows or {}, self.log)

    def acquire(self) -> _StubAcquire:
        return _StubAcquire(self._conn)


# --- LLM double + signal (for the run_method end-to-end trace test) ---------


@dataclass
class _Usage:
    prompt_tokens: int = 10
    completion_tokens: int = 5
    reasoning_tokens: int = 0


@dataclass
class _Response:
    content: str = ""
    usage: _Usage | None = None


class _CapturingLLM:
    subprovider = "openai"

    def __init__(self) -> None:
        self.last_user_prompt: str | None = None

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None,
                            system=None, **kwargs) -> Any:
        self.last_user_prompt = messages[-1]["content"] if messages else ""
        finding = {"title": "t", "body": "b", "confidence": 0.5, "evidence": [], "tags": []}
        return _Response(content=json.dumps(finding), usage=_Usage())


def _signal(geo: list[str] | None = None, tags: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "title": "Some event",
        "produced_at": "2026-06-18T00:00:00+00:00",
        "source_url": "https://example.invalid/x",
        "data": {"summary": "an observation"},
        "geo": geo or [],
        "tags": tags or [],
    }


def _descriptor_with_grounding(sources: list[str]):
    """A minimal valid inline_target descriptor opting into grounding."""
    from legba.data.schemas.analyst import AnalystDescriptor

    body: dict[str, Any] = {
        "identity": {
            "id": "leadership_transition",
            "name": "Leadership-Transition Risk Unit",
            "schema_uri": "legba/analyst/1.0.0",
            "version": "0" * 16,
            "kind": "inline_target",
            "type_signature": {
                "input_type": "legba.runtime.SignalList",
                "output_type": "legba.runtime.Finding",
            },
            "state": "active",
            "owner": "t",
        },
        "subscription": {"substrate": {"direct_queries": False}},
        "method": {
            "kind": "llm_planner",
            "prompt_module": "legba.runtime.analyst_method:_DEFAULT_SYSTEM",
            "llm": {"primary": {"factory_kind": "stack_ref", "raw": "llm.x",
                                "expected_family": "llm_provider"}},
        },
        "cadence": {"fallback_schedule": "0 */6 * * *"},
        "grounding": {"enabled": True, "scope": ["target_geo", "slice_entities"],
                      "sources": sources},
    }
    return AnalystDescriptor.model_validate(body, strict=False)


_US_FACT_ROW = {
    "subject": "United States", "predicate": "head of state", "value": "Donald Trump",
    "valid_from": datetime(2025, 1, 20, tzinfo=timezone.utc),
    "source_type": "seed", "confidence": 0.95,
}


# ---------------------------------------------------------------------------
# resolve_world_context — the resolver method
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_world_context_maps_hit_to_chunk():
    embedder = _StubEmbedder()
    qdrant = _FakeQdrant([_chunk_point(id="c-42")])
    resolver = SubstrateGroundingResolver(
        pg_pool=_StubPool(), embedder=embedder, qdrant=qdrant,
        world_context_collection="world_context",
    )
    chunks = await resolver.resolve_world_context("succession in country X", limit=30)
    assert len(chunks) == 1
    assert chunks[0].chunk_id == "c-42"
    assert chunks[0].title == "Succession primer"
    assert "succession dynamics" in chunks[0].text
    # The query was embedded and the search hit the world_context collection.
    assert embedder.embedded == ["succession in country X"]
    assert qdrant.last_call is not None
    assert qdrant.last_call["collection_name"] == "world_context"
    assert qdrant.last_call["query"] == [0.1, 0.2, 0.3, 0.4]


@pytest.mark.asyncio
async def test_resolve_world_context_empty_collection_returns_no_chunks():
    """Honesty: an EMPTY collection (no hits) yields no chunks → no block."""
    resolver = SubstrateGroundingResolver(
        pg_pool=_StubPool(), embedder=_StubEmbedder(), qdrant=_FakeQdrant([]),
    )
    chunks = await resolver.resolve_world_context("anything", limit=30)
    assert chunks == []
    assert build_world_context_block(chunks) is None


@pytest.mark.asyncio
async def test_resolve_world_context_no_embedder_or_qdrant_returns_empty():
    pool = _StubPool()
    # No embedder → no RAG.
    r1 = SubstrateGroundingResolver(pg_pool=pool, qdrant=_FakeQdrant([_chunk_point()]))
    assert await r1.resolve_world_context("q", limit=30) == []
    # No qdrant → no RAG.
    r2 = SubstrateGroundingResolver(pg_pool=pool, embedder=_StubEmbedder())
    assert await r2.resolve_world_context("q", limit=30) == []


@pytest.mark.asyncio
async def test_resolve_world_context_empty_query_short_circuits():
    embedder = _StubEmbedder()
    resolver = SubstrateGroundingResolver(
        pg_pool=_StubPool(), embedder=embedder, qdrant=_FakeQdrant([_chunk_point()]),
    )
    assert await resolver.resolve_world_context("   ", limit=30) == []
    assert embedder.embedded == []  # no embed round-trip on an empty query


@pytest.mark.asyncio
async def test_resolve_world_context_embed_failure_degrades():
    resolver = SubstrateGroundingResolver(
        pg_pool=_StubPool(), embedder=_RaisingEmbedder(), qdrant=_FakeQdrant([_chunk_point()]),
    )
    assert await resolver.resolve_world_context("q", limit=30) == []


@pytest.mark.asyncio
async def test_resolve_world_context_search_failure_degrades():
    resolver = SubstrateGroundingResolver(
        pg_pool=_StubPool(), embedder=_StubEmbedder(), qdrant=_RaisingQdrant(),
    )
    assert await resolver.resolve_world_context("q", limit=30) == []


@pytest.mark.asyncio
async def test_resolve_world_context_drops_empty_text_chunk():
    """A hit with no readable text contributes no prior (dropped)."""
    empty = _FakePoint(id="e", payload={"title": "t", "text": "  "}, score=0.5)
    resolver = SubstrateGroundingResolver(
        pg_pool=_StubPool(), embedder=_StubEmbedder(), qdrant=_FakeQdrant([empty]),
    )
    assert await resolver.resolve_world_context("q", limit=30) == []


@pytest.mark.asyncio
async def test_resolve_world_context_clamps_to_chunk_ceiling():
    points = [_chunk_point(id=f"c-{i}", text=f"prior {i}") for i in range(20)]
    qdrant = _FakeQdrant(points)
    resolver = SubstrateGroundingResolver(
        pg_pool=_StubPool(), embedder=_StubEmbedder(), qdrant=qdrant,
    )
    await resolver.resolve_world_context("q", limit=30)
    # The requested limit (30) is clamped to the RAG ceiling (2) at the search
    # (DQ Phase-2 tune: 6 -> 2 to shrink the uncited-interpretation leak surface).
    assert qdrant.last_call["limit"] == 2


# ---------------------------------------------------------------------------
# RELEVANCE FLOOR + COUNTRY FILTER — the two cheap retrieval guards (S5-T3)
# ---------------------------------------------------------------------------


class _FilteringQdrant:
    """Async Qdrant stand-in that HONORS ``score_threshold`` and a ``countries``
    MatchAny ``query_filter`` (like the real server), so a test can assert BOTH
    the filter that was passed AND that only matching chunks come back."""

    def __init__(self, points: list[_FakePoint]) -> None:
        self._points = points
        self.last_call: dict[str, Any] | None = None

    async def query_points(self, **kwargs: Any) -> _FakeQueryResponse:
        self.last_call = kwargs
        pts = list(self._points)
        thr = kwargs.get("score_threshold")
        if thr is not None:
            # Real Qdrant excludes hits scoring BELOW the threshold (>= kept).
            pts = [p for p in pts if p.score is None or p.score >= thr]
        qf = kwargs.get("query_filter")
        if qf is not None:
            wanted = set(qf.must[0].match.any)
            pts = [
                p for p in pts
                if wanted & set(p.payload.get("countries") or [])
            ]
        return _FakeQueryResponse(pts)


def _country_point(
    *, id: str, countries: list[str], score: float, text: str = "a prior",
) -> _FakePoint:
    return _FakePoint(
        id=id,
        payload={"corpus": "world_context", "title": id, "text": text,
                 "countries": countries},
        score=score,
    )


@pytest.mark.asyncio
async def test_relevance_floor_drops_below_score_chunk_client_side():
    """A below-floor chunk is dropped even when the client IGNORES the server-side
    score_threshold (the plain fake returns everything) — the client-side backstop
    in _map_world_context_hits enforces the floor. The above-floor chunk survives."""
    below = _FakePoint(id="lo", payload={"title": "lo", "text": "weak"}, score=0.3)
    above = _FakePoint(id="hi", payload={"title": "hi", "text": "strong"}, score=0.7)
    resolver = SubstrateGroundingResolver(
        pg_pool=_StubPool(), embedder=_StubEmbedder(),
        qdrant=_FakeQdrant([below, above]),  # ignores score_threshold
    )
    chunks = await resolver.resolve_world_context("q", limit=30)
    assert [c.chunk_id for c in chunks] == ["hi"]
    # The server-side floor was still requested (default 0.55, M22-recalibrated).
    assert resolver._qdrant.last_call["score_threshold"] == 0.55  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_relevance_floor_all_below_yields_no_block():
    """When ALL retrieved chunks fall below the floor → no chunks → NO block."""
    pts = [
        _FakePoint(id=f"c{i}", payload={"title": f"c{i}", "text": "weak"}, score=0.2)
        for i in range(4)
    ]
    resolver = SubstrateGroundingResolver(
        pg_pool=_StubPool(), embedder=_StubEmbedder(), qdrant=_FakeQdrant(pts),
    )
    chunks = await resolver.resolve_world_context("q", limit=30)
    assert chunks == []
    assert build_world_context_block(chunks) is None


@pytest.mark.asyncio
async def test_country_desk_filters_to_target_country_and_returns_only_its_chunks():
    """A single-country desk passes a ``countries`` MatchAny for its ISO-2 and
    (server honoring the filter) retrieves ONLY that country's chunks."""
    us = _country_point(id="us-1", countries=["us", "US"], score=0.7)
    ir = _country_point(id="ir-1", countries=["ir", "IR"], score=0.7)
    qdrant = _FilteringQdrant([us, ir])
    resolver = SubstrateGroundingResolver(
        pg_pool=_StubPool(), embedder=_StubEmbedder(), qdrant=qdrant,
    )
    chunks = await resolver.resolve_world_context(
        "q", limit=30, target_id="country_g20_us",
    )
    # Only the US chunk comes back — the Iran chunk is filtered out server-side.
    assert [c.chunk_id for c in chunks] == ["us-1"]
    # The query carried a MatchAny country filter over the ``countries`` field for
    # the desk's ISO-2 (both cases the Lane-4 loader may have stored).
    qf = qdrant.last_call["query_filter"]
    assert qf is not None
    cond = qf.must[0]
    assert cond.key == "countries"
    assert list(cond.match.any) == ["us", "US"]


@pytest.mark.asyncio
async def test_watch_desk_derives_iso2_from_last_slug_segment():
    """A watch-tier desk (country_watch_ir) scopes to its trailing ISO-2 'ir'."""
    ir = _country_point(id="ir-1", countries=["ir", "IR"], score=0.7)
    us = _country_point(id="us-1", countries=["us", "US"], score=0.7)
    qdrant = _FilteringQdrant([ir, us])
    resolver = SubstrateGroundingResolver(
        pg_pool=_StubPool(), embedder=_StubEmbedder(), qdrant=qdrant,
    )
    chunks = await resolver.resolve_world_context(
        "q", limit=30, target_id="country_watch_ir",
    )
    assert [c.chunk_id for c in chunks] == ["ir-1"]
    assert list(qdrant.last_call["query_filter"].must[0].match.any) == ["ir", "IR"]


@pytest.mark.asyncio
async def test_meta_no_target_run_applies_no_country_filter():
    """A meta / no-target run (world_assessor) applies NO country filter — the
    global picture is legitimate — but the relevance floor still rides."""
    a = _country_point(id="a", countries=["us", "US"], score=0.7)
    b = _country_point(id="b", countries=["ir", "IR"], score=0.7)
    qdrant = _FilteringQdrant([a, b])
    resolver = SubstrateGroundingResolver(
        pg_pool=_StubPool(), embedder=_StubEmbedder(), qdrant=qdrant,
    )
    chunks = await resolver.resolve_world_context("q", limit=30, target_id=None)
    # No country filter → BOTH countries' chunks are eligible.
    assert {c.chunk_id for c in chunks} == {"a", "b"}
    assert qdrant.last_call["query_filter"] is None
    assert qdrant.last_call["score_threshold"] == 0.55


@pytest.mark.asyncio
async def test_region_target_applies_no_country_filter():
    """A non-single-country target (region composer) resolves to no ISO-2 → NO
    filter (degrade to the global view rather than an empty match)."""
    a = _country_point(id="a", countries=["fr", "FR"], score=0.7)
    qdrant = _FilteringQdrant([a])
    resolver = SubstrateGroundingResolver(
        pg_pool=_StubPool(), embedder=_StubEmbedder(), qdrant=qdrant,
    )
    chunks = await resolver.resolve_world_context(
        "q", limit=30, target_id="region_europe",
    )
    assert [c.chunk_id for c in chunks] == ["a"]
    assert qdrant.last_call["query_filter"] is None


def test_world_context_country_filter_values_helper():
    """The ISO-2 derivation: single-country desks → [iso2, ISO2]; meta / region /
    malformed → None (no filter, never an empty match)."""
    assert world_context_country_filter_values("country_g20_us") == ["us", "US"]
    assert world_context_country_filter_values("country_watch_ir") == ["ir", "IR"]
    # Meta / no-target and non-single-country targets → no filter.
    assert world_context_country_filter_values(None) is None
    assert world_context_country_filter_values("region_europe") is None
    assert world_context_country_filter_values("thematic_energy") is None


def test_world_context_min_score_env_override(monkeypatch):
    """The floor is env-overridable; a blank / malformed value falls back."""
    monkeypatch.delenv("LEGBA_WORLD_CONTEXT_MIN_SCORE", raising=False)
    assert world_context_min_score() == 0.55
    monkeypatch.setenv("LEGBA_WORLD_CONTEXT_MIN_SCORE", "0.62")
    assert world_context_min_score() == 0.62
    monkeypatch.setenv("LEGBA_WORLD_CONTEXT_MIN_SCORE", "not-a-float")
    assert world_context_min_score() == 0.55


# ---------------------------------------------------------------------------
# build_world_context_block — the BACKGROUND PRIORS renderer
# ---------------------------------------------------------------------------


def test_build_world_context_block_header_and_non_citable():
    chunk = GroundingWorldContextChunk(
        chunk_id="c-1", title="Succession primer", section="Overview",
        source_url=None, text="Watch coalition arithmetic and elite defections.",
        score=0.9,
    )
    block = build_world_context_block([chunk])
    assert block is not None
    # The EXACT operator-facing header string is present verbatim.
    assert "BACKGROUND PRIORS (context, not evidence — do not cite)" in block
    assert "NOT" in block and "citable" in block  # the non-citable warning
    assert "Succession primer — Overview" in block
    assert "coalition arithmetic" in block
    # The ground-truth header must NOT leak into this block.
    assert "AUTHORITATIVE CURRENT CONTEXT" not in block


def test_build_world_context_block_none_when_empty():
    assert build_world_context_block([]) is None


def test_build_world_context_block_trims_long_chunk_body():
    long_text = "x" * 5000
    chunk = GroundingWorldContextChunk(
        chunk_id="c-1", title="T", section=None, source_url=None,
        text=long_text, score=0.5,
    )
    block = build_world_context_block([chunk])
    assert block is not None
    # The per-chunk char cap trims the body (ellipsis) so it can't blow context.
    assert "x" * 5000 not in block
    assert "…" in block


def test_build_world_context_block_stops_at_block_cap_but_admits_one():
    chunks = [
        GroundingWorldContextChunk(
            chunk_id=f"c-{i}", title=f"T{i}", section=None, source_url=None,
            text="y" * 600, score=0.5,
        )
        for i in range(6)
    ]
    block = build_world_context_block(chunks, block_char_cap=50)
    assert block is not None
    # The first chunk is always admitted (never an empty header)...
    assert "T0" in block
    # ...but the tiny block cap stops the fold before the rest crowd the context.
    assert "T1" not in block and "T5" not in block


# ---------------------------------------------------------------------------
# Deps-builder hook — block placement + chunk-id recording
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hook_background_priors_below_authoritative_and_records_ids():
    pool = _StubPool(fetch_rows={"facts": [_US_FACT_ROW], "nexuses": []})
    qdrant = _FakeQdrant([_chunk_point(id="chunk-xyz")])
    embedder = _StubEmbedder()
    hook = _build_grounding_hook(
        _descriptor_with_grounding(["substrate", "vector:world_context"]),
        pg_pool=pool, embedder=embedder, qdrant=qdrant,
    )
    assert hook is not None
    sink: list[str] = []
    out = await hook(
        [_signal(geo=["United States"])],
        {"target_id": "country_g20_us", GROUNDING_RAG_CHUNK_SINK_KEY: sink},
    )
    assert out is not None
    # Both blocks present; BACKGROUND PRIORS sits BELOW the authoritative preamble.
    assert "AUTHORITATIVE CURRENT CONTEXT" in out
    assert "BACKGROUND PRIORS (context, not evidence — do not cite)" in out
    assert out.index("AUTHORITATIVE CURRENT CONTEXT") < out.index("BACKGROUND PRIORS")
    # The retrieved chunk id was recorded into the caller's trace sink.
    assert sink == ["chunk-xyz"]
    # M22 FOCUSED query recipe: "<target country> <theme>" — the target country
    # LEADS (its Factbook chunks are the retrieval target), the theme is the
    # corpus-facet phrase (here derived from the descriptor name, no rag_theme set),
    # and the noisy unit-name + person-entity pile is DROPPED. The query TEXT is
    # what we embedded; the vector reached qdrant.
    assert len(embedder.embedded) == 1
    rag_query = embedder.embedded[0]
    assert rag_query.startswith("United States")
    assert "leadership transition" in rag_query.lower()
    # No unit-name noise, no person-entity dilution in the focused query.
    assert "Risk Unit" not in rag_query
    assert qdrant.last_call is not None
    assert qdrant.last_call["collection_name"] == "world_context"


@pytest.mark.asyncio
async def test_per_run_kill_switch_env_stops_injection_without_rebuild(monkeypatch):
    """FIX A (M22): a rollback via the env pin suppresses vector:world_context on the
    SAME cached hook's NEXT run — no rebuild / restart / deps eviction. The pre-fix
    build-time-only check would keep injecting until an unrelated restart."""
    monkeypatch.delenv("LEGBA_WORLD_CONTEXT_DISABLED_UNITS", raising=False)
    monkeypatch.delenv("LEGBA_RAG_ROLLBACK_STATE", raising=False)
    pool = _StubPool(fetch_rows={"facts": [_US_FACT_ROW], "nexuses": []})
    qdrant = _FakeQdrant([_chunk_point(id="chunk-xyz")])
    hook = _build_grounding_hook(
        _descriptor_with_grounding(["substrate", "vector:world_context"]),
        pg_pool=pool, embedder=_StubEmbedder(), qdrant=qdrant,
    )
    assert hook is not None
    # Run 1 — enabled: BACKGROUND PRIORS injected.
    out1 = await hook([_signal(geo=["United States"])], {"target_id": "country_g20_us"})
    assert "BACKGROUND PRIORS (context, not evidence — do not cite)" in out1
    # Roll the unit back (as rag_watch --enforce would) WITHOUT rebuilding the hook.
    monkeypatch.setenv("LEGBA_WORLD_CONTEXT_DISABLED_UNITS", "leadership_transition")
    # Run 2 — SAME cached closure, next run: no BACKGROUND PRIORS; substrate intact.
    out2 = await hook([_signal(geo=["United States"])], {"target_id": "country_g20_us"})
    assert "AUTHORITATIVE CURRENT CONTEXT" in out2
    assert "BACKGROUND PRIORS" not in out2
    # And re-enabling (clear the pin) resumes injection on the very same closure.
    monkeypatch.delenv("LEGBA_WORLD_CONTEXT_DISABLED_UNITS", raising=False)
    out3 = await hook([_signal(geo=["United States"])], {"target_id": "country_g20_us"})
    assert "BACKGROUND PRIORS (context, not evidence — do not cite)" in out3


@pytest.mark.asyncio
async def test_per_run_kill_switch_via_record_rollback_state_file(monkeypatch, tmp_path):
    """FIX A (M22): the FULL actuator loop — rag_rollback.record_rollback writes the
    persisted state, and the SAME cached hook stops injecting on its next run (the
    path rag_watch --enforce / an auto-trigger drives)."""
    from legba.runtime.rag_rollback import record_rollback

    state = tmp_path / "rag_rollback.json"
    monkeypatch.setenv("LEGBA_RAG_ROLLBACK_STATE", str(state))
    monkeypatch.delenv("LEGBA_WORLD_CONTEXT_DISABLED_UNITS", raising=False)
    hook = _build_grounding_hook(
        _descriptor_with_grounding(["substrate", "vector:world_context"]),
        pg_pool=_StubPool(fetch_rows={"facts": [_US_FACT_ROW], "nexuses": []}),
        embedder=_StubEmbedder(), qdrant=_FakeQdrant([_chunk_point(id="chunk-xyz")]),
    )
    assert hook is not None
    out1 = await hook([_signal(geo=["United States"])], {"target_id": "country_g20_us"})
    assert "BACKGROUND PRIORS" in out1
    # Auto-rollback fires (the actuator persists the unit) — no hook rebuild.
    assert record_rollback("leadership_transition", reasons=["faith -0.09"]) == str(state)
    out2 = await hook([_signal(geo=["United States"])], {"target_id": "country_g20_us"})
    assert "AUTHORITATIVE CURRENT CONTEXT" in out2
    assert "BACKGROUND PRIORS" not in out2


@pytest.mark.asyncio
async def test_hook_empty_collection_yields_no_block_no_ids():
    """Empty collection → no BACKGROUND PRIORS block; substrate block unaffected."""
    pool = _StubPool(fetch_rows={"facts": [_US_FACT_ROW], "nexuses": []})
    hook = _build_grounding_hook(
        _descriptor_with_grounding(["substrate", "vector:world_context"]),
        pg_pool=pool, embedder=_StubEmbedder(), qdrant=_FakeQdrant([]),
    )
    assert hook is not None
    sink: list[str] = []
    out = await hook(
        [_signal(geo=["United States"])],
        {"target_id": "country_g20_us", GROUNDING_RAG_CHUNK_SINK_KEY: sink},
    )
    assert out is not None
    assert "AUTHORITATIVE CURRENT CONTEXT" in out
    assert "BACKGROUND PRIORS" not in out
    assert sink == []


@pytest.mark.asyncio
async def test_hook_without_vector_source_never_queries_qdrant():
    """A descriptor NOT opting into vector:world_context never touches qdrant."""
    qdrant = _FakeQdrant([_chunk_point()])
    hook = _build_grounding_hook(
        _descriptor_with_grounding(["substrate"]),
        pg_pool=_StubPool(fetch_rows={"facts": [_US_FACT_ROW], "nexuses": []}),
        embedder=_StubEmbedder(), qdrant=qdrant,
    )
    assert hook is not None
    out = await hook([_signal(geo=["United States"])], {"target_id": "country_g20_us"})
    assert out is not None
    assert "BACKGROUND PRIORS" not in out
    assert qdrant.last_call is None  # never searched


@pytest.mark.asyncio
async def test_hook_vector_only_no_qdrant_degrades_to_no_block():
    """vector:world_context declared but no qdrant wired → no block (degrade)."""
    hook = _build_grounding_hook(
        _descriptor_with_grounding(["vector:world_context"]),
        pg_pool=_StubPool(), embedder=_StubEmbedder(), qdrant=None,
    )
    assert hook is not None
    out = await hook([_signal(geo=["United States"])], {"target_id": "country_g20_us"})
    assert out is None  # nothing resolved → no preamble at all


# ---------------------------------------------------------------------------
# End-to-end through inline_target.run_method — the trace event carries ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_method_inject_preamble_trace_carries_chunk_ids():
    pool = _StubPool(fetch_rows={"facts": [_US_FACT_ROW], "nexuses": []})
    hook = _build_grounding_hook(
        _descriptor_with_grounding(["substrate", "vector:world_context"]),
        pg_pool=pool, embedder=_StubEmbedder(), qdrant=_FakeQdrant([_chunk_point(id="cid-9")]),
    )
    llm = _CapturingLLM()
    deps = InlineTargetDeps(llm=llm, grounding_hook=hook)
    result = await run_method(
        [_signal(geo=["United States"])], {"target_id": "country_g20_us"}, deps,
    )
    assert result.finding is not None
    # The prompt carries the non-citable priors block.
    assert "BACKGROUND PRIORS" in (llm.last_user_prompt or "")
    # The inject_preamble trace step records the retrieved chunk ids.
    ground = [s for s in result.intermediate_steps if s.get("kind") == "inject_preamble"]
    assert len(ground) == 1
    assert ground[0]["world_context_chunk_ids"] == ["cid-9"]


@pytest.mark.asyncio
async def test_run_method_substrate_only_trace_has_no_chunk_ids():
    """A substrate-only grounding leaves the inject_preamble event unchanged
    (no world_context_chunk_ids key)."""
    pool = _StubPool(fetch_rows={"facts": [_US_FACT_ROW], "nexuses": []})
    hook = _build_grounding_hook(
        _descriptor_with_grounding(["substrate"]),
        pg_pool=pool, embedder=_StubEmbedder(), qdrant=_FakeQdrant([_chunk_point()]),
    )
    llm = _CapturingLLM()
    deps = InlineTargetDeps(llm=llm, grounding_hook=hook)
    result = await run_method(
        [_signal(geo=["United States"])], {"target_id": "country_g20_us"}, deps,
    )
    ground = [s for s in result.intermediate_steps if s.get("kind") == "inject_preamble"]
    assert len(ground) == 1
    assert "world_context_chunk_ids" not in ground[0]
