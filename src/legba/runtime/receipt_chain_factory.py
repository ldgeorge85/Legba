# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-analyst :class:`RuntimeReceiptChain` factory + process-global cache.

L-107 §7 (Mnemosyne D5 alignment) requires every analyst run to extend a
tamper-evident SHA-256 chain over its run receipts. The chain head is
hydrated from the last ``analyst_traces`` row for that analyst at first
use (``ZERO_HASH`` when no prior rows exist); each subsequent ``record``
links via ``prev_receipt_hash``.

A single :class:`RuntimeReceiptChain` instance must span all runs of a
given analyst within a process so concurrent runs serialize via the
chain's per-analyst lock (see :class:`RuntimeReceiptChain.record`). The
runtime resolver — :func:`legba.runtime.analyst_deps_builder.build_analyst_run_method`
— calls :func:`build_receipt_chain_for_analyst` to obtain (or construct)
the per-analyst chain, then stuffs it into ``_AnalystDeps.receipt_chain``.

Cache shape
-----------
The cache is keyed by ``analyst_id`` (not ``analyst_id+version``):
version bumps still extend the same chain because the prompt /
analyst_version both fold into the canonical receipt payload — the
chain follows the analyst identity, not the descriptor version. This
matches the lock keying inside :class:`RuntimeReceiptChain`.

The cache is also keyed by the ``id()`` of the ``pg_pool`` so a test
process that swaps pools between fixtures gets fresh chains (tests
rebuild the analyst_traces table per pool; reusing an old chain would
return a stale head pointer). Production has a single pool so this
secondary key collapses.
"""

from __future__ import annotations

import asyncpg

from ..data.provenance.receipts import RuntimeReceiptChain


__all__ = [
    "build_receipt_chain_for_analyst",
    "clear_receipt_chain_cache",
]


# Process-global cache. Key = (id(pg_pool), analyst_id). Value = the
# per-analyst chain instance. Constructed lazily on first ask.
_CHAINS: dict[tuple[int, str], RuntimeReceiptChain] = {}


def build_receipt_chain_for_analyst(
    analyst_id: str,
    analyst_version: str,
    *,
    pg_pool: asyncpg.Pool,
) -> RuntimeReceiptChain:
    """Return the :class:`RuntimeReceiptChain` for ``analyst_id``.

    First call for a given ``(pg_pool, analyst_id)`` pair constructs a
    fresh chain; subsequent calls return the cached instance so the
    chain's in-memory head pointer + per-analyst lock are reused across
    runs.

    ``analyst_version`` is accepted (and required by the signature
    contract) but not used as a cache key — the chain spans versions
    of the same analyst per the Mnemosyne D5 alignment in L-107 §7.
    The argument is retained so future implementations can stamp the
    version into per-chain telemetry without a signature change.
    """
    # ``analyst_version`` intentionally not part of the cache key — see
    # module docstring. Reference it once so static analyzers see the use.
    _ = analyst_version

    key = (id(pg_pool), analyst_id)
    chain = _CHAINS.get(key)
    if chain is None:
        # Bind the analyst id so the chain's D11 fork-tip diagnostic
        # (``head_tip_count``) can default to it. Per-run methods still take an
        # explicit analyst_id; this is diagnostic-only.
        chain = RuntimeReceiptChain(pg_pool, analyst_id=analyst_id)
        _CHAINS[key] = chain
    return chain


def clear_receipt_chain_cache() -> None:
    """Drop every cached chain.

    Test hook only. Production never clears — the cache lives for the
    process lifetime and the chain's lock + head pointer must persist
    across actor invocations.
    """
    _CHAINS.clear()
