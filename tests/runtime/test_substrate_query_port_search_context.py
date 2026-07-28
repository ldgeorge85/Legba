# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S5-T4 ``search_context`` — RAG over the Lane-4 curated corpora.

INFRA-FREE (no live Postgres / Qdrant). Exercises the port method contract:

  * with an embedder + a Qdrant client, a free-text ``search_context`` embeds
    the query, cosine-searches BOTH corpus collections (``world_context`` /
    ``tradecraft``) when no ``corpus`` filter is given, and merges the hits by
    score, returning the Lane-4 payload metadata (corpus / doc_id / title /
    section / countries / source_url / effective_date);
  * a ``corpus`` filter narrows to one collection; an unknown corpus is a
    structured error (no embed round-trip);
  * a ``country`` filter builds a Qdrant payload filter over the ``countries``
    array and threads it to the search;
  * the seam-#11 honesty contract holds — no embedder → ``no_embedder_wired``,
    an empty query short-circuits, an embed failure degrades to ``unavailable``;
  * a per-collection Qdrant error is skipped (the other corpus still
    contributes) rather than failing the whole call; an empty-text chunk is
    dropped;
  * REF HONESTY (W2-T4 residual): chunk ids come back ``ctx:``-prefixed —
    visibly NOT substrate UUIDs, excluded from substrate lineage by the
    loop's ``_coerce_uuid_list`` — with the parallel ``context_refs`` list +
    ``refs_note`` stating they are non-citable background refs.

``pg_pool`` is never touched by ``search_context`` — a bare sentinel stands in.
"""

from __future__ import annotations

from typing import Any

import pytest

from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort

_WC = "test_world_context"
_TC = "test_tradecraft"


# ---------------------------------------------------------------------------
# In-process fakes
# ---------------------------------------------------------------------------


class _FakePoint:
    def __init__(self, *, id: str, payload: dict[str, Any], score: float) -> None:
        self.id = id
        self.payload = payload
        self.score = score


class _FakeQueryResponse:
    def __init__(self, points: list[_FakePoint]) -> None:
        self.points = points


class _FakeQdrant:
    """Async Qdrant stand-in keyed by collection; records every call.

    ``raise_for`` names a collection whose search raises (per-collection error
    path). Any collection with no configured points returns an empty hit set.
    """

    def __init__(
        self,
        points_by_collection: dict[str, list[_FakePoint]],
        *,
        raise_for: str | None = None,
    ) -> None:
        self._points = points_by_collection
        self._raise_for = raise_for
        self.calls: list[dict[str, Any]] = []

    async def query_points(self, **kwargs: Any) -> _FakeQueryResponse:
        self.calls.append(kwargs)
        coll = kwargs["collection_name"]
        if self._raise_for is not None and coll == self._raise_for:
            raise RuntimeError("qdrant collection unavailable")
        return _FakeQueryResponse(self._points.get(coll, []))


class _StubEmbedder:
    dim = 4

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector
        self.embed_calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return list(self._vector)


class _RaisingEmbedder:
    dim = 4

    async def embed(self, text: str) -> list[float]:  # noqa: ARG002
        raise RuntimeError("embedding backend unreachable")


def _port(qdrant: Any, embedder: Any) -> PostgresQdrantSubstrateQueryPort:
    return PostgresQdrantSubstrateQueryPort(
        pg_pool=object(),  # never touched by search_context
        qdrant_client=qdrant,
        embedder=embedder,
        world_context_collection=_WC,
        tradecraft_collection=_TC,
    )


def _chunk(cid: str, corpus: str, score: float, **overrides: Any) -> _FakePoint:
    payload = {
        "corpus": corpus,
        "doc_id": f"{corpus}-doc",
        "title": f"{corpus} title",
        "section": "S1",
        "countries": ["ir"],
        "source_url": "https://example.invalid/doc",
        "effective_date": "2026-01-01",
        "text": f"prior chunk from {corpus}",
    }
    payload.update(overrides)
    return _FakePoint(id=cid, payload=payload, score=score)


# ---------------------------------------------------------------------------
# Happy path — both corpora, merged by score
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_context_searches_both_corpora_and_merges_by_score() -> None:
    qdrant = _FakeQdrant({
        _WC: [_chunk("wc-1", "world_context", 0.70)],
        _TC: [_chunk("tc-1", "tradecraft", 0.90)],
    })
    embedder = _StubEmbedder([0.1, 0.2, 0.3, 0.4])
    port = _port(qdrant, embedder)

    out = await port.search_context(query="iran succession", k=5)

    # Embedded once; both collections queried.
    assert embedder.embed_calls == ["iran succession"]
    queried = {c["collection_name"] for c in qdrant.calls}
    assert queried == {_WC, _TC}
    assert out["corpora_searched"] == ["world_context", "tradecraft"]
    assert out["backing"] == "qdrant_cosine"

    # Merged newest-similarity-first (tradecraft 0.90 before world_context 0.70).
    assert out["count"] == 2
    assert [r["chunk_id"] for r in out["rows"]] == ["ctx:tc-1", "ctx:wc-1"]
    # W2-T4 REF HONESTY: chunk refs are ctx:-prefixed (non-UUID, so the
    # consult loop's _coerce_uuid_list EXCLUDES them from substrate lineage
    # by design) and mirrored on the explicit context_refs list.
    assert out["refs"] == ["ctx:tc-1", "ctx:wc-1"]
    assert out["context_refs"] == ["ctx:tc-1", "ctx:wc-1"]
    assert "non-citable" in out["refs_note"]

    # The Lane-4 payload metadata rides on each row.
    top = out["rows"][0]
    assert top["corpus"] == "tradecraft"
    assert top["doc_id"] == "tradecraft-doc"
    assert top["title"] == "tradecraft title"
    assert top["section"] == "S1"
    assert top["countries"] == ["ir"]
    assert top["source_url"] == "https://example.invalid/doc"
    assert top["effective_date"] == "2026-01-01"
    assert top["score"] == pytest.approx(0.90)
    assert "prior chunk" in top["text"]


@pytest.mark.asyncio
async def test_search_context_corpus_filter_narrows_to_one_collection() -> None:
    qdrant = _FakeQdrant({
        _WC: [_chunk("wc-1", "world_context", 0.70)],
        _TC: [_chunk("tc-1", "tradecraft", 0.90)],
    })
    port = _port(qdrant, _StubEmbedder([1.0, 0.0, 0.0, 0.0]))

    out = await port.search_context(query="SAT method", corpus="tradecraft", k=5)

    assert out["corpora_searched"] == ["tradecraft"]
    assert {c["collection_name"] for c in qdrant.calls} == {_TC}
    assert [r["chunk_id"] for r in out["rows"]] == ["ctx:tc-1"]


@pytest.mark.asyncio
async def test_search_context_corpus_filter_is_case_insensitive() -> None:
    qdrant = _FakeQdrant({_WC: [_chunk("wc-1", "world_context", 0.5)]})
    port = _port(qdrant, _StubEmbedder([1.0, 0.0, 0.0, 0.0]))

    out = await port.search_context(query="q", corpus="World_Context")

    assert out["corpora_searched"] == ["world_context"]
    assert out["count"] == 1


@pytest.mark.asyncio
async def test_search_context_country_filter_threads_qdrant_filter() -> None:
    qdrant = _FakeQdrant({
        _WC: [_chunk("wc-1", "world_context", 0.7)],
        _TC: [],
    })
    port = _port(qdrant, _StubEmbedder([0.0, 1.0, 0.0, 0.0]))

    out = await port.search_context(query="q", country="ir")

    assert out["count"] == 1
    # Every collection search carried a non-None query_filter (the country
    # MatchAny over the ``countries`` payload array).
    assert qdrant.calls, "expected at least one collection search"
    for call in qdrant.calls:
        qf = call["query_filter"]
        assert qf is not None
        # The filter targets the countries field with the requested value.
        cond = qf.must[0]
        assert cond.key == "countries"
        assert list(cond.match.any) == ["ir"]


@pytest.mark.asyncio
async def test_search_context_k_is_clamped_and_merged_across_corpora() -> None:
    qdrant = _FakeQdrant({
        _WC: [_chunk("wc-1", "world_context", 0.6), _chunk("wc-2", "world_context", 0.4)],
        _TC: [_chunk("tc-1", "tradecraft", 0.9), _chunk("tc-2", "tradecraft", 0.8)],
    })
    port = _port(qdrant, _StubEmbedder([0.5, 0.5, 0.0, 0.0]))

    out = await port.search_context(query="q", k=3)

    # 4 hits total, clamped to k=3, ordered by score.
    assert out["k"] == 3
    assert out["count"] == 3
    assert [r["chunk_id"] for r in out["rows"]] == ["ctx:tc-1", "ctx:tc-2", "ctx:wc-1"]


# ---------------------------------------------------------------------------
# Structured error + honesty contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_context_unknown_corpus_is_structured_error_no_embed() -> None:
    qdrant = _FakeQdrant({})
    embedder = _StubEmbedder([1.0, 0.0, 0.0, 0.0])
    port = _port(qdrant, embedder)

    out = await port.search_context(query="q", corpus="bogus")

    assert "error" in out and "unknown corpus" in out["error"]
    assert out["rows"] == [] and out["refs"] == [] and out["context_refs"] == []
    # No embed round-trip, no Qdrant call.
    assert embedder.embed_calls == []
    assert qdrant.calls == []


@pytest.mark.asyncio
async def test_search_context_without_embedder_reports_unavailable() -> None:
    qdrant = _FakeQdrant({_WC: [_chunk("wc-1", "world_context", 0.5)]})
    port = _port(qdrant, None)

    out = await port.search_context(query="q")

    assert out["unavailable"] is True
    assert "no_embedder_wired" in out["reason"]
    assert out["rows"] == []
    assert qdrant.calls == []


@pytest.mark.asyncio
async def test_search_context_empty_query_skips_embed() -> None:
    qdrant = _FakeQdrant({_WC: [_chunk("wc-1", "world_context", 0.5)]})
    embedder = _StubEmbedder([1.0, 0.0, 0.0, 0.0])
    port = _port(qdrant, embedder)

    out = await port.search_context(query="   ")

    assert out["rows"] == [] and out["refs"] == [] and out["context_refs"] == []
    assert out["note"] == "empty_query"
    assert embedder.embed_calls == []
    assert qdrant.calls == []


@pytest.mark.asyncio
async def test_search_context_embed_failure_degrades() -> None:
    qdrant = _FakeQdrant({_WC: [_chunk("wc-1", "world_context", 0.5)]})
    port = _port(qdrant, _RaisingEmbedder())

    out = await port.search_context(query="anything")

    assert out["unavailable"] is True
    assert "embed_failed" in out["reason"]
    assert qdrant.calls == []


@pytest.mark.asyncio
async def test_search_context_per_collection_error_is_skipped() -> None:
    qdrant = _FakeQdrant(
        {
            _WC: [_chunk("wc-1", "world_context", 0.7)],
            _TC: [_chunk("tc-1", "tradecraft", 0.9)],
        },
        raise_for=_TC,
    )
    port = _port(qdrant, _StubEmbedder([0.3, 0.3, 0.3, 0.3]))

    out = await port.search_context(query="q")

    # tradecraft raised → skipped; world_context still contributes.
    assert out["corpora_searched"] == ["world_context"]
    assert [r["chunk_id"] for r in out["rows"]] == ["ctx:wc-1"]


@pytest.mark.asyncio
async def test_search_context_drops_empty_text_chunk() -> None:
    qdrant = _FakeQdrant({
        _WC: [
            _chunk("wc-good", "world_context", 0.8),
            _chunk("wc-empty", "world_context", 0.9, text="   "),
        ],
        _TC: [],
    })
    port = _port(qdrant, _StubEmbedder([1.0, 0.0, 0.0, 0.0]))

    out = await port.search_context(query="q")

    # The empty-text chunk is dropped even though it scored higher.
    assert [r["chunk_id"] for r in out["rows"]] == ["ctx:wc-good"]
