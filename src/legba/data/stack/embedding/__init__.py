# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.stack.embedding — embedding service stack-handler namespace.

Retained as an empty package after L-205. The pre-reshape in-process
``BgeM3EmbeddingHandler`` (BAAI/bge-m3 via ``sentence-transformers``) retired
when the embedding path moved to the hosted ``bge-m3`` endpoint
(``embed.primary.openai_compat`` stack component, vLLM
``/v1/embeddings``). Future local-model handlers can land in this package
alongside the hosted clients.

Git history preserves the BGE-M3 module + its tests for archaeology.
"""

from __future__ import annotations

__all__: list[str] = []
