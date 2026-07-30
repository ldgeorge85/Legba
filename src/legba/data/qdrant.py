# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.qdrant — wrapper around `qdrant-client`.

Only the `legba_signals` collection survives per `design/legba_storage_layout.md`
§2.2 + §3.4. The three dormant collections (`legba_short_term`,
`legba_long_term`, `legba_facts`) retire — they had zero indexed vectors and
under the descriptor model episodic memory is per-actor analyst state, not
a global collection.

Collection config: 1024-dim cosine, BGE-M3 embeddings. Per-target collections
(per L-091 §6) are materialized on demand by the L-114 provenance-tagging
work — this module exposes the helpers; the lifecycle hooks land later.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

try:
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.http import models as qmodels
except Exception:  # pragma: no cover — qdrant-client must be installed
    AsyncQdrantClient = None  # type: ignore[assignment]
    qmodels = None  # type: ignore[assignment]

from .config import QdrantConfig

logger = logging.getLogger(__name__)

DORMANT_COLLECTIONS: tuple[str, ...] = (
    "legba_short_term",
    "legba_long_term",
    "legba_facts",
)


class QdrantStore:
    """Async wrapper around `AsyncQdrantClient`.

    Exposes only what the L-001 substrate factor needs:
      * ensure_signals_collection() — idempotent create with BGE-M3 dims
      * ensure_world_context_collection() / ensure_tradecraft_collection() —
        the Lane-4 RAG corpus collections (S5-T2), same dim/distance
      * retire_dormant_collections() — drop the three deprecated collections
      * ensure_target_collection(target_id) — collection-per-target pattern
        per L-091 §6 (kept here; the lifecycle hook is L-114)
      * upsert_points / search / count_doc_points / delete_doc_points — the
        Lane-4 corpus loader's point I/O (all qmodels handling lives here)
    """

    def __init__(self, cfg: QdrantConfig):
        if AsyncQdrantClient is None:  # pragma: no cover
            raise RuntimeError("qdrant-client is not installed")
        self._cfg = cfg
        self._client: AsyncQdrantClient | None = None

    @classmethod
    def from_env(cls) -> "QdrantStore":
        return cls(QdrantConfig.from_env())

    @property
    def client(self) -> "AsyncQdrantClient":
        if self._client is None:
            raise RuntimeError("QdrantStore not connected")
        return self._client

    @property
    def cfg(self) -> QdrantConfig:
        return self._cfg

    async def connect(self) -> None:
        if self._client is not None:
            return
        self._client = AsyncQdrantClient(
            host=self._cfg.host,
            port=self._cfg.port,
            grpc_port=self._cfg.grpc_port,
            api_key=self._cfg.api_key,
            https=self._cfg.https,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    # ------------------------------------------------------------------
    # Collection management
    # ------------------------------------------------------------------

    async def list_collections(self) -> list[str]:
        result = await self.client.get_collections()
        return [c.name for c in result.collections]

    async def _ensure_collection(self, name: str) -> bool:
        """Idempotently create a BGE-M3 1024-dim cosine collection.

        Returns True if newly created, False if it already existed. The single
        create path used by every ensure_* helper so dim/distance never drift
        between collections (they all share the one bge-m3 embedder).
        """
        existing = await self.list_collections()
        if name in existing:
            return False
        await self.client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(
                size=self._cfg.signals_dim,
                distance=qmodels.Distance.COSINE,
            ),
        )
        return True

    async def ensure_signals_collection(self) -> bool:
        """Create the canonical `legba_signals` collection if it doesn't exist.

        Returns True if newly created, False if it already existed.
        """
        return await self._ensure_collection(self._cfg.signals_collection)

    async def ensure_world_context_collection(self) -> bool:
        """Create the `world_context` RAG corpus collection (Lane-4, S5-T2).

        The country/topic-priors corpus (unstructured briefs, doctrine
        summaries keyed to places/actors). Same 1024-dim cosine / bge-m3 as
        `legba_signals`. Name matches the `vector:world_context` grounding
        source token. Returns True if newly created, False if it existed.
        """
        return await self._ensure_collection(self._cfg.world_context_collection)

    async def ensure_tradecraft_collection(self) -> bool:
        """Create the `tradecraft` RAG corpus collection (Lane-4, S5-T2).

        The HOW-to-analyze corpus (analytic standards, SAT handbooks,
        doctrine). Separate collection because its retrieval key is the
        question/method, not the target. Same dim/distance as `legba_signals`.
        Returns True if newly created, False if it existed.
        """
        return await self._ensure_collection(self._cfg.tradecraft_collection)

    async def retire_dormant_collections(self) -> list[str]:
        """Drop the three dormant Qdrant collections per L-091 §3.4.

        Returns the list of names actually dropped.
        """
        existing = set(await self.list_collections())
        dropped: list[str] = []
        for name in DORMANT_COLLECTIONS:
            if name in existing:
                await self.client.delete_collection(collection_name=name)
                dropped.append(name)
        return dropped

    async def ensure_target_collection(self, target_id: str) -> str:
        """Create a per-target collection on demand (L-091 §6 pattern).

        Naming convention: `legba_target__<target_id>`. Same dim/distance as
        the canonical signals collection so the same embedding model serves
        both. The runtime hook is L-114; this is the substrate-side helper.
        """
        name = f"legba_target__{target_id}"
        existing = await self.list_collections()
        if name not in existing:
            await self.client.create_collection(
                collection_name=name,
                vectors_config=qmodels.VectorParams(
                    size=self._cfg.signals_dim,
                    distance=qmodels.Distance.COSINE,
                ),
            )
        return name

    async def collection_info(self, name: str) -> dict[str, Any] | None:
        try:
            info = await self.client.get_collection(collection_name=name)
        except Exception:
            return None
        return {
            "vectors_count": getattr(info, "vectors_count", None),
            "points_count": getattr(info, "points_count", None),
            "indexed_vectors_count": getattr(info, "indexed_vectors_count", None),
            "status": getattr(info, "status", None),
        }

    # ------------------------------------------------------------------
    # Point I/O passthroughs (used by the Lane-4 corpus loader, S5-T2)
    #
    # Kept on the store so PointStruct / query-version handling lives in
    # ONE place — callers pass plain tuples/dicts and never touch qmodels.
    # ------------------------------------------------------------------

    async def upsert_points(
        self,
        collection_name: str,
        points: Iterable[tuple[str, list[float], dict[str, Any]]],
    ) -> int:
        """Upsert ``(point_id, vector, payload)`` tuples; return the count.

        Idempotent by design: a deterministic ``point_id`` (the Lane-4 loader
        derives it from the chunk natural key) makes a re-upsert overwrite the
        same point in place rather than duplicating it.
        """
        structs = [
            qmodels.PointStruct(id=pid, vector=list(vec), payload=dict(payload))
            for pid, vec, payload in points
        ]
        if not structs:
            return 0
        await self.client.upsert(collection_name=collection_name, points=structs)
        return len(structs)

    async def retrieve_vectors(
        self, collection_name: str, ids: Iterable[str]
    ) -> dict[str, list[float]]:
        """Stored vectors for the given point ids: ``{point_id: vector}``.

        Points not present are simply absent from the result (the caller
        degrades per-point). Named-vector points return their first vector.
        Used by the ``claim_watch`` matcher to read signal vectors BACK by id
        (point id = signal id, the signal_embedder contract) instead of
        re-embedding bodies.
        """
        id_list = [str(i) for i in ids]
        if not id_list:
            return {}
        points = await self.client.retrieve(
            collection_name=collection_name,
            ids=id_list,
            with_payload=False,
            with_vectors=True,
        )
        out: dict[str, list[float]] = {}
        for p in points or []:
            vec = getattr(p, "vector", None)
            if isinstance(vec, dict):  # named-vector collections
                vec = next(iter(vec.values()), None)
            if vec:
                out[str(getattr(p, "id", ""))] = list(vec)
        return out

    def _doc_filter(self, corpus: str, doc_id: str) -> "qmodels.Filter":
        """Payload filter selecting every chunk of one ``(corpus, doc_id)``."""
        return qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="corpus", match=qmodels.MatchValue(value=corpus)
                ),
                qmodels.FieldCondition(
                    key="doc_id", match=qmodels.MatchValue(value=doc_id)
                ),
            ]
        )

    async def count_doc_points(
        self, collection_name: str, *, corpus: str, doc_id: str
    ) -> int:
        """Count stored chunks for one document (best-effort; 0 on any error)."""
        try:
            res = await self.client.count(
                collection_name=collection_name,
                count_filter=self._doc_filter(corpus, doc_id),
                exact=True,
            )
        except Exception:  # collection may not exist yet, etc.
            return 0
        return int(getattr(res, "count", 0) or 0)

    async def delete_doc_points(
        self, collection_name: str, *, corpus: str, doc_id: str
    ) -> int:
        """Delete every chunk of one ``(corpus, doc_id)``; return the prior count.

        DELETE-EXCEPTION (Lane-4 ``--mode=force``): vector rows are DERIVED,
        re-embeddable artifacts, so the platform's no-hard-delete rule is
        RELAXED here only — a force reload deletes the doc's chunks and
        re-embeds. Structured facts/nexuses (Lanes 1-3) keep their temporal
        supersession; the vector plane can be rebuilt from source at any time.
        """
        prior = await self.count_doc_points(
            collection_name, corpus=corpus, doc_id=doc_id
        )
        await self.client.delete(
            collection_name=collection_name,
            points_selector=qmodels.FilterSelector(
                filter=self._doc_filter(corpus, doc_id)
            ),
        )
        return prior

    async def search(
        self,
        collection_name: str,
        *,
        query_embedding: list[float],
        limit: int = 10,
        query_filter: "qmodels.Filter | None" = None,
    ) -> list[dict[str, Any]]:
        """Cosine search a collection by raw vector; return scored rows.

        Rows are ``{"id", "score", "payload"}``. Supports both the modern
        ``query_points`` (qdrant-client >= 1.10) and the legacy ``search``
        surface so the module isn't pinned to one client version (mirrors
        ``substrate_query_port.vector_search_by_embedding``).
        """
        if hasattr(self.client, "query_points"):
            resp = await self.client.query_points(
                collection_name=collection_name,
                query=list(query_embedding),
                limit=int(limit),
                query_filter=query_filter,
                with_payload=True,
            )
            hits = getattr(resp, "points", None) or []
        else:  # pragma: no cover — legacy client
            hits = await self.client.search(
                collection_name=collection_name,
                query_vector=list(query_embedding),
                limit=int(limit),
                query_filter=query_filter,
                with_payload=True,
            )
        rows: list[dict[str, Any]] = []
        for hit in hits or []:
            rows.append(
                {
                    "id": str(getattr(hit, "id", "")),
                    "score": float(getattr(hit, "score", 0.0)),
                    "payload": getattr(hit, "payload", None) or {},
                }
            )
        return rows
