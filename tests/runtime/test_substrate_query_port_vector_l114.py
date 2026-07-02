# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-114 embedder-through-port wiring for the consult ``vector_search``.

These tests are deliberately INFRA-FREE — no live Postgres / Qdrant. They
exercise the threading contract (S5-T1):

  * with an embedder threaded through the port, free-text ``vector_search``
    embeds the query (a real :class:`HostedEmbeddingClient` wired to a mocked
    ``/v1/embeddings`` endpoint — the embed round-trip) then runs the Qdrant
    cosine search via ``vector_search_by_embedding``, and NO LONGER returns the
    ``no_embedder_wired`` refusal shape;
  * with NO embedder (the embedding service wasn't provisioned) the method
    still honestly reports ``unavailable=True`` / ``no_embedder_wired``;
  * an embed-backend failure degrades to ``unavailable`` / ``embed_failed``
    rather than fabricating a vector; and
  * an empty query short-circuits (no embed round-trip) to an empty result.

``pg_pool`` is never touched by ``vector_search`` (it reads only the embedder
+ the Qdrant client), so a bare sentinel stands in for it here.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from legba.runtime.embedding_factory import HostedEmbeddingClient
from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort


# ---------------------------------------------------------------------------
# In-process fakes
# ---------------------------------------------------------------------------


class _FakePoint:
    """A single scored Qdrant hit (mirrors the ``QueryResponse.points`` item)."""

    def __init__(self, *, id: str, payload: dict[str, Any], score: float) -> None:
        self.id = id
        self.payload = payload
        self.score = score


class _FakeQueryResponse:
    def __init__(self, points: list[_FakePoint]) -> None:
        self.points = points


class _FakeQdrant:
    """Minimal async Qdrant stand-in exposing ``query_points``.

    Records the last call so a test can assert the embedded vector reached
    the search; returns a fixed hit set.
    """

    def __init__(self, points: list[_FakePoint]) -> None:
        self._points = points
        self.last_call: dict[str, Any] | None = None

    async def query_points(self, **kwargs: Any) -> _FakeQueryResponse:
        self.last_call = kwargs
        return _FakeQueryResponse(self._points)


def _hosted_embedder(vector: list[float]) -> HostedEmbeddingClient:
    """A real HostedEmbeddingClient whose ``/v1/embeddings`` HTTP call is mocked.

    Exercises the genuine embed round-trip (payload shape, bearer header,
    OpenAI-shaped response parse) rather than a hand-rolled ``embed`` stub.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/v1/embeddings")
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": vector}],
                "model": "bge-m3",
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            },
        )

    inner = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return HostedEmbeddingClient(
        endpoint="https://llm.example.internal/v1",
        api_key="test-bearer",
        model_name="bge-m3",
        dim=4,
        normalize=True,
        batch_size=8,
        http_client=inner,
    )


class _RaisingEmbedder:
    """An embedder whose backend is down (embed raises)."""

    dim = 4

    async def embed(self, text: str) -> list[float]:  # noqa: ARG002
        raise RuntimeError("embedding backend unreachable")


# ---------------------------------------------------------------------------
# vector_search — embedder present (the L-114 happy path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vector_search_with_embedder_reaches_qdrant() -> None:
    """An embedder threaded in → embed round-trip + Qdrant search, no refusal."""
    hit = _FakePoint(
        id="sig-1",
        payload={
            "external_id": "ext-1",
            "source_id": "src-1",
            "target_id": "br-energy",
        },
        score=0.87,
    )
    qdrant = _FakeQdrant([hit])
    embedder = _hosted_embedder([0.9, 0.1, 0.0, 0.0])
    port = PostgresQdrantSubstrateQueryPort(
        pg_pool=object(),  # never touched by vector_search
        qdrant_client=qdrant,
        embedder=embedder,
        signals_collection="legba_test_l114__signals",
    )
    try:
        result = await port.vector_search(query="Itaipu hydroelectric", limit=5)
    finally:
        await embedder.aclose()

    # The refusal shape is GONE.
    assert "unavailable" not in result
    assert result.get("reason") is None or "no_embedder_wired" not in result.get("reason", "")
    # The embedded vector reached Qdrant (the embed round-trip ran).
    assert qdrant.last_call is not None
    assert qdrant.last_call["query"] == [0.9, 0.1, 0.0, 0.0]
    assert qdrant.last_call["collection_name"] == "legba_test_l114__signals"
    # Rows come from the Qdrant hits; free-text query + backing are echoed.
    assert result["query"] == "Itaipu hydroelectric"
    assert result["backing"] == "qdrant_cosine"
    assert result["rows"][0]["signal_id"] == "sig-1"
    assert result["rows"][0]["target_id"] == "br-energy"
    assert result["rows"][0]["score"] == pytest.approx(0.87)
    assert "sig-1" in result["refs"]


@pytest.mark.asyncio
async def test_vector_search_empty_query_skips_embed() -> None:
    """An empty query returns an empty result WITHOUT an embed round-trip."""
    qdrant = _FakeQdrant([])
    embedder = _hosted_embedder([1.0, 0.0, 0.0, 0.0])
    port = PostgresQdrantSubstrateQueryPort(
        pg_pool=object(),
        qdrant_client=qdrant,
        embedder=embedder,
        signals_collection="legba_test_l114__signals",
    )
    try:
        result = await port.vector_search(query="   ", limit=5)
    finally:
        await embedder.aclose()

    assert result["rows"] == []
    assert result["refs"] == []
    assert result["note"] == "empty_query"
    assert result["backing"] == "qdrant_cosine"
    # No embed round-trip, so Qdrant was never queried.
    assert qdrant.last_call is None


@pytest.mark.asyncio
async def test_vector_search_embed_failure_degrades() -> None:
    """An embed-backend failure degrades to unavailable, never a fake vector."""
    qdrant = _FakeQdrant([])
    port = PostgresQdrantSubstrateQueryPort(
        pg_pool=object(),
        qdrant_client=qdrant,
        embedder=_RaisingEmbedder(),
        signals_collection="legba_test_l114__signals",
    )
    result = await port.vector_search(query="anything", limit=5)

    assert result["unavailable"] is True
    assert "embed_failed" in result["reason"]
    assert result["rows"] == []
    # Never reached Qdrant (no vector to search with).
    assert qdrant.last_call is None


# ---------------------------------------------------------------------------
# vector_search — no embedder (the honest seam #11 fallback, retained)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vector_search_without_embedder_reports_unavailable() -> None:
    """No embedder wired → the honest ``no_embedder_wired`` refusal, no fabrication."""
    qdrant = _FakeQdrant([])
    port = PostgresQdrantSubstrateQueryPort(
        pg_pool=object(),
        qdrant_client=qdrant,
        embedder=None,
        signals_collection="legba_test_l114__signals",
    )
    result = await port.vector_search(query="Itaipu hydroelectric", limit=5)

    assert result["unavailable"] is True
    assert "no_embedder_wired" in result["reason"]
    assert result["rows"] == []
    assert result["collection"] == "legba_test_l114__signals"
    # Never queried Qdrant.
    assert qdrant.last_call is None
