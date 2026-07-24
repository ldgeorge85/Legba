# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.rag — the opportunistic-RAG vector plane (Stream 5).

Home for the corpus-side machinery that turns operator-supplied documents into
retrievable Qdrant chunks:

  * :mod:`legba.data.rag.chunker` — a heading-aware, ~400-800-token chunker.
  * :mod:`legba.data.rag.lane4_loader` — the Lane-4 manual-ingest loader that
    chunks + embeds + upserts a batch's ``docs`` lane into the ``world_context``
    / ``tradecraft`` collections, riding the existing ``seed_batches`` ledger
    for idempotency.

The retrieval side (inline grounding of `vector:world_context`, the
`search_context` tool) hangs off these but lands later (RAG plan phases 3-4).
"""

from __future__ import annotations

from .chunker import Chunk, chunk_text, estimate_tokens
from .lane4_loader import (
    CORPUS_COLLECTIONS,
    VectorLoadResult,
    contextual_embedding_input,
    load_vector_batch,
)

__all__ = [
    "Chunk",
    "chunk_text",
    "estimate_tokens",
    "CORPUS_COLLECTIONS",
    "VectorLoadResult",
    "contextual_embedding_input",
    "load_vector_batch",
]
