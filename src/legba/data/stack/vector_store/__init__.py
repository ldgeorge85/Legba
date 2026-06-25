# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.stack.vector_store — Phase-2 vector store stack handlers (L-121).

Per L-102 §1 (kind handler contracts), each stack component family is backed
by a handler class that conforms to the `KindHandler` protocol. This
sub-package owns the vector-store family.

Single concrete kind for now:

  * `vector.qdrant` — `QdrantVectorStoreHandler` (`kind = "vector_store"`).

Wraps the L-001 `legba.data.qdrant.QdrantStore` connection wrapper. Adds:

  * Operations surface (`upsert`, `search`, `delete`, `create_collection`,
    `delete_collection`, `list_collections`) typed against pydantic
    models — `VectorPoint`, `ScoredPoint` — so callers don't need to import
    Qdrant client types.
  * Per-target collection support via the `legba_target_<target_id>`
    convention (per L-091 §6 + this task's brief). `legba_signals`
    is the shared default collection.
  * Lifecycle hooks (`on_configure`, `on_activate`, `on_pause`, `on_retire`)
    against a minimal local context shape; runtime context (L-103) will
    expand this without breaking the contract — the slice we use is
    forward-compatible (descriptor id, typed config, optional logger).
  * Healthcheck consistent with L-102 `HandlerHealth` (state + last_success
    + detail) layered on top of `get_collections()`.
  * Filter pass-through: callers pass either a Qdrant model `Filter` or a
    plain dict; the handler normalises into `qmodels.Filter`. Supported
    clauses are documented in `qdrant.py` (match / range / has_id / geo).

This handler does not own actor scheduling, budget reporting, or trace
emission — those land in Phase 5 (L-160 / L-163 / L-107). The handler
exposes the operations surface those Phase-5 components compose over.
"""

from __future__ import annotations

from .qdrant import (
    ConfigureContext,
    HandlerHealth,
    QdrantVectorStoreConfig,
    QdrantVectorStoreHandler,
    RuntimeContext,
    ScoredPoint,
    VectorPoint,
    VectorStoreFilter,
    collection_name_for_target,
)

__all__ = [
    "ConfigureContext",
    "HandlerHealth",
    "QdrantVectorStoreConfig",
    "QdrantVectorStoreHandler",
    "RuntimeContext",
    "ScoredPoint",
    "VectorPoint",
    "VectorStoreFilter",
    "collection_name_for_target",
]
