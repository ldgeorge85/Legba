# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.stack — Phase-2 stack component handlers.

Per L-102 §1 (kind handler contracts) and L-120 (Phase 2 task), each stack
component family (LLM provider, vector store, embedding service, etc.) is
backed by a *handler class* that conforms to the `KindHandler` protocol
extended with family-specific methods.

This package contains the family-by-family handler implementations:

  * `legba.data.stack.llm`       — LLM provider handlers (L-120; this drop).
  * `legba.data.stack.vector`    — Vector store handlers (L-121; future).
  * `legba.data.stack.embedding` — Embedding service handlers (L-122; future).
  * `legba.data.stack.proxy`     — Proxy pool handlers (L-123; future).
  * `legba.data.stack.nats`      — NATS handlers (L-124; future).
  * `legba.data.stack.postgres`  — Postgres handlers (L-125; future).

The Phase-1 `StackHealthDispatcher` (`legba.data.registry.health`) ships
in-tree lightweight healthcheckers per stack kind. Phase-2 handlers
*supersede* those for components they own by re-registering themselves
against the dispatcher's `HEALTH_CHECKERS` map. The L-120 LLM handlers do
NOT call the model on every poll (no token burn per healthcheck) — they
verify endpoint TCP reachability + vault key resolution only. Model-burning
pings happen only when an operator explicitly asks for them.
"""

from __future__ import annotations

__all__: list[str] = []

# Phase-4 architectural correction (2026-05-22): hosted NLP service.
# Defensive import so callers that don't need it (registry-only) aren't
# charged the httpx-async warm-up cost on stack package import.
try:                                                                # pragma: no cover
    from .nlp_service import (  # noqa: F401
        NlpServiceAuthError,
        NlpServiceClient,
        NlpServiceUnavailable,
    )
except Exception:                                                   # pragma: no cover
    pass
else:                                                               # pragma: no cover
    __all__.extend([
        "NlpServiceAuthError",
        "NlpServiceClient",
        "NlpServiceUnavailable",
    ])
