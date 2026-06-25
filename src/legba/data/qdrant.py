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
      * retire_dormant_collections() — drop the three deprecated collections
      * ensure_target_collection(target_id) — collection-per-target pattern
        per L-091 §6 (kept here; the lifecycle hook is L-114)
      * upsert / search passthroughs
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

    async def ensure_signals_collection(self) -> bool:
        """Create the canonical `legba_signals` collection if it doesn't exist.

        Returns True if newly created, False if it already existed.
        """
        existing = await self.list_collections()
        if self._cfg.signals_collection in existing:
            return False
        await self.client.create_collection(
            collection_name=self._cfg.signals_collection,
            vectors_config=qmodels.VectorParams(
                size=self._cfg.signals_dim,
                distance=qmodels.Distance.COSINE,
            ),
        )
        return True

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
