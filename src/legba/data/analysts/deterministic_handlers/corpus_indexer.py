# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``corpus_indexer`` sub-handler — async indexing of signals into OpenSearch.

The INDEX PLANE of the signal-content-depth program. Downstream mining wants
cheap LEXICAL retrieval over the WHOLE signal body (BM25 keyword search + keyword
/date facets), not just the analytic slice — so this sub-handler projects each
signal into the OpenSearch full-text corpus (``legba_signals_corpus``). Signals
already live structured in Postgres and, for the RAG corpus, as vectors in
Qdrant; this is the third leg — a full-text mining substrate.

It is an ASYNC SWEEP (never inline in ingest — ingestion must stay cheap): every
time the bound ``deterministic`` analyst fires (cadence), it indexes the next
batch of un-indexed signals, draining the backlog. Indexing is CHEAP (no LLM), so
the batch is much bigger than the summarizer's.

Per tick (mirrors ``signal_summarizer`` / ``entity_resolution`` idempotency):

  * SELECT the next batch ``WHERE indexed_at IS NULL ORDER BY fetched_at DESC``
    (newest-first — fresh signals reach the corpus within a tick of ingest, per
    the summarizer's own recency rationale). NO modality filter — the corpus
    indexes ALL modalities.

    ``indexed_at IS NULL`` doubles as a DIRTY-QUEUE, not just a first-index gate.
    A signal whose INDEXABLE content changes after it was first indexed re-enters
    this scan by having its ``indexed_at`` nulled by the writer — e.g.
    ``signal_summarizer`` nulls it when it writes ``payload.distilled_body`` (the
    two are separate sweeps, so a signal indexed BEFORE it is summarized would
    otherwise keep a summary-less corpus doc forever). The re-index OVERWRITES in
    place (``_id`` = the signal id). Keeping the marker as the sole predicate lets
    the partial index ``idx_signals_unindexed`` serve both first-index and
    re-index with no extra index / no per-tick full scan.

    DIRTY-MARKER CONTRACT (binds every future content writer — translation, entity
    enrichment, …). To re-queue a signal for the corpus, a writer MUST, in the SAME
    UPDATE: (1) ``SET indexed_at = NULL`` AND (2) ``SET updated_at = now()``. BOTH
    are load-bearing. Nulling ``indexed_at`` re-enters the row into this scan;
    bumping ``updated_at`` is what protects the re-null from the version-guarded
    stamp below (``_STAMP_BULK_SQL``). If a writer nulls ``indexed_at`` WITHOUT
    bumping ``updated_at``, an in-flight indexer batch that snapshotted the same
    (unchanged) ``updated_at`` will stamp the row and clobber the re-null → the
    lost-update race reopens and the doc goes stale forever. ``signal_summarizer``
    honors both (``_WRITE_SUMMARY_SQL``); any new writer must too.
  * Project each row via :func:`legba.data.opensearch.signal_to_doc` (``_id`` =
    the signal id, so a re-index OVERWRITES in place).
  * ``ensure_index`` (idempotent) then ``bulk_index`` the batch.
  * VERSION-GUARDED stamp of ``indexed_at = now()`` in ONE bulk UPDATE — but only
    for rows whose ``updated_at`` still matches the value snapshotted at SELECT, so
    a row a concurrent content writer re-dirtied mid-I/O is left un-stamped and
    re-indexed next tick (closes the lost-update race — see ``_STAMP_BULK_SQL``).
    Un-raced rows drain out of the partial index and are never re-scanned.

Degrade-not-break: if the OpenSearchStore is absent from ``deps.extras`` (the
index plane isn't wired — dep missing / OS unreachable at deps-build), the tick
NO-OPs with a warning (like the summarizer's no-LLM guard), leaving rows
un-indexed for a tick where the store is wired. A transport failure during the
bulk request likewise leaves the batch UN-STAMPED (retried next tick).

Output ``data`` keys (the cadence receipt the operator reads):
    examined        int — rows pulled this run
    indexed         int — rows successfully indexed into the corpus this run
    failures        int — examined rows the bulk request could not index
    requeued_dirty  int — rows whose version (updated_at) MOVED between our SELECT
                          and our stamp — some concurrent writer touched them
                          mid-window (a summarizer re-dirty, or any other updated_at
                          bump). Left un-stamped → re-indexed next tick. Benign +
                          observable. Usually 0.
    skipped_no_store int — 1 when the OpenSearchStore was not wired (else 0)
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ...opensearch import CORPUS_INDEX_MAPPING, signal_to_doc
from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "corpus_indexer"

#: The key under which the runtime stashes the connected OpenSearchStore on
#: ``StandardDeps.extras`` for this sweep (wired in analyst_deps_builder when the
#: bound sub-handler is ``corpus_indexer``).
OS_DEPS_EXTRA_KEY = "corpus_indexer_os"

#: How many signals to SELECT + index per tick. Indexing is cheap (no LLM), so
#: this is much bigger than the summarizer's batch — it drains the ~105k backlog
#: quickly and then idles on the small new-signal arrival rate.
_DEFAULT_BATCH = 1000

#: NEWEST-first scan of the un-indexed pool (WHERE matches the partial index
#: idx_signals_unindexed; btree supports the reverse scan). Newest-first so fresh
#: signals reach the corpus within ~a tick of ingest (summarizer rationale). The
#: selected columns feed signal_to_doc; ALL modalities are indexed (no filter).
#: ``indexed_at IS NULL`` is BOTH the first-index gate AND the re-index dirty-queue
#: (a content writer nulls indexed_at to re-enqueue — see the module docstring).
#: ``updated_at`` is selected purely as the optimistic-concurrency version token
#: for the guarded stamp below (NOT used by signal_to_doc).
_SELECT_BATCH_SQL = """
    SELECT id, source_id, geo, tags, entity_classes, language, modality,
           retention_class, canonical_url, source_credibility, fetched_at,
           raw_provenance, payload, updated_at
      FROM signals
     WHERE indexed_at IS NULL
     ORDER BY fetched_at DESC
     LIMIT $1
"""

#: VERSION-GUARDED bulk stamp — closes the lost-update race with a concurrent
#: content writer. This sweep SELECTs a snapshot, does slow external I/O
#: (bulk_index to OpenSearch), THEN stamps indexed_at. A content writer (e.g.
#: signal_summarizer) that writes distilled_body + nulls indexed_at IN THAT WINDOW
#: would otherwise be clobbered by a blind ``SET indexed_at = now()`` (last-writer-
#: wins on the marker) → its change lost, the corpus doc stale forever (nothing
#: re-nulls it). The guard stamps ONLY rows whose ``updated_at`` still equals the
#: value we snapshotted at SELECT: a row re-dirtied in the window has a DIFFERENT
#: updated_at, so it is NOT stamped → indexed_at stays NULL → re-indexed next tick
#: with the fresh content (converges in one extra tick; the stale doc is corrected
#: then). $1 = uuid[] of examined ids; $2 = timestamptz[] of the updated_at each
#: row carried at SELECT (parallel arrays — same order). ``IS NOT DISTINCT FROM``
#: so a NULL updated_at (should not happen — NOT NULL column — but defensive)
#: compares correctly.
_STAMP_BULK_SQL = """
    UPDATE signals AS s
       SET indexed_at = now()
      FROM unnest($1::uuid[], $2::timestamptz[]) AS b(id, seen_updated_at)
     WHERE s.id = b.id
       AND s.updated_at IS NOT DISTINCT FROM b.seen_updated_at
"""


def _parse_update_count(status: Any) -> int:
    """Parse asyncpg's command tag (e.g. ``"UPDATE 998"``) → the row count.

    Returns 0 on any parse failure (never raises) so a surprising tag can't wedge
    the sweep — the count feeds an observability counter only."""
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


def _resolve_store(deps: Any | None) -> Any | None:
    """Pull the connected OpenSearchStore off ``deps.extras`` (or ``None``).

    Injected by
    :func:`legba.runtime.analyst_deps_builder._wire_corpus_indexer_os` when the
    bound sub-handler is ``corpus_indexer``. Absent → the sweep no-ops that tick."""
    if deps is None:
        return None
    extras = getattr(deps, "extras", None)
    if not isinstance(extras, Mapping):
        return None
    return extras.get(OS_DEPS_EXTRA_KEY)


async def _sweep_batch(pool: Any, *, store: Any, index: str, batch_limit: int) -> dict[str, int]:
    """Index the next batch of un-indexed signals into the OpenSearch corpus.

    The connection is NOT held across the bulk request — the batch is SELECTed
    once, indexed, then a fresh connection stamps the marker. A transport failure
    in ``ensure_index`` / ``bulk_index`` RAISES to the caller BEFORE the stamp, so
    the batch is left un-stamped and retried next tick (the ``_id`` overwrite makes
    the retry idempotent). Per-doc mapping errors are counted (not re-raised) and
    the row is still stamped (a poison doc must not wedge the sweep)."""
    counters = {
        "examined": 0,
        "indexed": 0,
        "failures": 0,
        "requeued_dirty": 0,
        "skipped_no_store": 0,
    }

    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_BATCH_SQL, batch_limit)
    if not rows:
        return counters
    counters["examined"] = len(rows)

    # Idempotent + cheap (a HEAD; create only on a fresh / dropped index).
    await store.ensure_index(index, CORPUS_INDEX_MAPPING)

    docs = [signal_to_doc(r) for r in rows]
    indexed = await store.bulk_index(index, docs)
    counters["indexed"] = int(indexed)
    counters["failures"] = len(rows) - int(indexed)

    # VERSION-GUARDED stamp: pass the id + the updated_at snapshotted at SELECT as
    # PARALLEL arrays (same order). A row a concurrent content writer re-dirtied in
    # our I/O window has a moved updated_at → the guard skips it → indexed_at stays
    # NULL → re-indexed next tick with the fresh content. See _STAMP_BULK_SQL.
    ids = [r["id"] for r in rows]
    seen_updated_at = [r["updated_at"] for r in rows]
    async with pool.acquire() as conn:
        status = await conn.execute(_STAMP_BULK_SQL, ids, seen_updated_at)
    stamped = _parse_update_count(status)
    # Rows left un-stamped were re-dirtied mid-window; they re-index next tick.
    # Surface the count so the race is OBSERVABLE, never a silent stale doc.
    counters["requeued_dirty"] = max(0, len(rows) - stamped)

    return counters


def _build_finding(counters: Mapping[str, int]) -> FindingPayload:
    title = (
        f"Corpus indexer: indexed {counters.get('indexed', 0)} signal(s), "
        f"examined {counters.get('examined', 0)}, "
        f"{counters.get('failures', 0)} failed"
    )
    body = "\n".join(f"{k}={v}" for k, v in counters.items())
    tags = ["deterministic", "corpus_indexer"]
    if counters.get("indexed", 0):
        tags.append("indexed")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={"sub_handler": SUB_HANDLER_NAME, **dict(counters)},
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Sweeps the substrate directly via ``deps.pg_pool`` (the ``inputs`` slice is
    ignored — the unit of work is "the next batch of un-indexed signals").
    ``deps is None`` (unit-test path) yields a zeroed run. Usage is always zeroed
    (deterministic kind, no LLM)."""
    counters: dict[str, int] = {
        "examined": 0,
        "indexed": 0,
        "failures": 0,
        "requeued_dirty": 0,
        "skipped_no_store": 0,
    }
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        store = _resolve_store(deps)
        if store is None:
            # The index plane isn't wired (dep missing / OS unreachable at
            # deps-build). No-op this tick — leave rows un-indexed for a tick
            # where the store IS wired. Go LOUD so a mis-wire is observable.
            counters["skipped_no_store"] = 1
            logger.warning(
                "corpus_indexer.no_store — OpenSearchStore absent from "
                "deps.extras[%r]; the index plane did not wire (dep missing / OS "
                "unreachable). Signals left un-indexed this tick.",
                OS_DEPS_EXTRA_KEY,
            )
        else:
            batch_limit = int(options.get("batch_limit", _DEFAULT_BATCH))
            try:
                counters = await _sweep_batch(
                    pool, store=store, index=store.cfg.index, batch_limit=batch_limit
                )
            except Exception as exc:
                logger.warning("corpus_indexer.failed err=%s", exc)

    return AnalystMethodResult(
        finding=_build_finding(counters),
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle", "SUB_HANDLER_NAME", "OS_DEPS_EXTRA_KEY"]
