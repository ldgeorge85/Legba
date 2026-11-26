# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""LLM price-table data shapes — dep-free.

Extracted from :mod:`legba.data.stack.llm.base` so consumers that only
need the static pricing schema (e.g.
:mod:`legba.data.provenance.budget`) can import without pulling
``httpx`` through ``base.py``.

Why this exists as a separate module: ``httpx`` defines
``_CookieCompatRequest(urllib.request.Request)`` at module-load, which
the Temporal workflow sandbox rejects (RestrictedWorkflowAccessError
on ``urllib.request.Request.__mro_entries__``). Anything in the
import chain of a Temporal workflow worker (which includes
``legba.runtime.__init__`` → ``dapr_actors`` → ``data.provenance`` →
``provenance.budget``) would otherwise eagerly pull httpx and fail
workflow validation. Splitting the pricing schema out keeps the
write-side cost helper importable without the HTTP-client weight.

Re-exported by ``base.py`` so existing consumers (the provider
handlers) don't see an import-path break.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens. ``reasoning_per_m`` only set for reasoning models;
    Anthropic charges reasoning tokens at the output rate, so leave 0 there."""

    input_per_m: float
    output_per_m: float
    cache_read_per_m: float = 0.0
    cache_write_per_m: float = 0.0
    reasoning_per_m: float = 0.0


# ---------------------------------------------------------------------------
# Token totals — reasoning counted once
# ---------------------------------------------------------------------------

#: Subproviders whose parsed usage carries reasoning OUTSIDE
#: ``completion_tokens``. The Anthropic handler reads a separate
#: ``thinking_tokens`` figure and its own ``total_tokens`` adds it, so its
#: tallies add it too. Every other handler speaks the OpenAI-compatible wire,
#: where ``completion_tokens_details.reasoning_tokens`` is a SUBSET of
#: ``completion_tokens`` (the gateway's docs: reasoning tokens are "included
#: in completion_tokens and broken out"); adding it again counted a thinking
#: call's reasoning twice.
REASONING_SEPARATE_SUBPROVIDERS: frozenset[str] = frozenset({"anthropic"})


def reasoning_in_completion(subprovider: str) -> bool:
    """True when ``reasoning_tokens`` is already inside ``completion_tokens``."""
    return str(subprovider or "") not in REASONING_SEPARATE_SUBPROVIDERS


def call_token_total(
    subprovider: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int = 0,
) -> int:
    """Prompt + completion (+ reasoning only where it is reported separately).

    The one formula both token tallies use: the per-call ``ctx.budget`` report
    (``base.chat_complete``) and the ``budget_ledger.tokens_used`` increment
    (``provenance.budget.record_budget``).
    """
    total = int(prompt_tokens or 0) + int(completion_tokens or 0)
    if not reasoning_in_completion(subprovider):
        total += int(reasoning_tokens or 0)
    return total
