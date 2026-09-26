# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The OpenSearch corpus lane of the substrate query port.

``search_corpus`` (BM25 lexical recall over the whole raw body of every
ingested signal) and ``read_document`` (one document by id, across every id
namespace a planner can hold) are the port's only OpenSearch-backed readers,
and the 2026-09-16 consult review found both of them returning results the
model could neither cite nor read. Extracting them here keeps the repair
(citable refs, a bounded projection, a Postgres namespace fallback) in one
cohesive place instead of growing the port module past its size ceiling.

They stay FREE FUNCTIONS over an explicit ``(store, index, pool)`` rather than
methods: that is what lets the projection invariant in
:mod:`legba.runtime.corpus_read_projection` be exercised without standing up a
port. :class:`~legba.runtime.substrate_query_port.PostgresQdrantSubstrateQueryPort`
delegates to them and owns the Protocol surface.

Degrade-not-break throughout (the port's seam contract): no corpus wired →
the honest ``no_corpus_wired`` shape, never a connect attempt; a transport or
search failure is logged and folded into an ``error`` field rather than raised
into the consult loop.
"""

from __future__ import annotations

import logging
from typing import Any

from .corpus_read_projection import (
    citable_ref,
    project_corpus_document,
    project_corpus_hits,
    project_finding_row,
    project_signal_row,
)

logger = logging.getLogger(__name__)

#: Default / hard cap on ``search_corpus`` hits. Small by default, capped so a
#: runaway planner cannot pull the whole corpus in one round.
SEARCH_CORPUS_DEFAULT_SIZE = 10
SEARCH_CORPUS_MAX_SIZE = 50

#: The keyword-facet subset of the corpus mapping (see
#: :data:`legba.data.opensearch.CORPUS_INDEX_MAPPING`) a planner may term-filter
#: on. Any other key is dropped before the query is built — an arbitrary facet
#: would silently match nothing while looking like an honest empty.
CORPUS_FILTER_KEYS = (
    "geo",
    "tags",
    "source_id",
    "language",
    "modality",
    "entity_classes",
    "retention_class",
    "license_class",
)


async def search_corpus(
    store: Any,
    index: str,
    *,
    query: str,
    filters: dict[str, Any] | None = None,
    size: int = SEARCH_CORPUS_DEFAULT_SIZE,
) -> dict[str, Any]:
    """BM25 lexical search over the OpenSearch signal corpus (Stage 1).

    Runs a multi_match keyword search over the WHOLE raw signal body (index
    ``index``, ~106k docs) via :meth:`~legba.data.opensearch.OpenSearchStore.search`,
    with optional keyword term filters. Complements ``vector_search`` (dense
    cosine over the analytic slice) and ``search_signals`` (Postgres FTS over
    title + summary): this is cheap LEXICAL recall over the full text of
    EVERY ingested signal.

    ``filters`` maps a keyword facet → a scalar (term) or a list (terms);
    only the whitelisted keys in :data:`CORPUS_FILTER_KEYS` with a non-None
    value are honored (any other key is dropped before the query is built).
    ``size`` is clamped to ``[1, SEARCH_CORPUS_MAX_SIZE]``. A falsy ``query``
    is allowed — the store degrades to match_all so a filter-only browse
    works.

    HONESTY / degrade-not-break (mirrors ``vector_search``'s seam contract):
    no corpus wired → the honest ``no_corpus_wired`` shape (never connects);
    a transport/search failure is logged and folded into an ``error`` field
    rather than raising into the consult loop.
    """
    clamped = max(1, min(int(size), SEARCH_CORPUS_MAX_SIZE))

    # No OpenSearch store threaded through the port — honest unavailable
    # shape, never a connect attempt (the same contract the embedder readers
    # honor with ``no_embedder_wired``).
    if store is None:
        return {
            "rows": [],
            "refs": [],
            "count": 0,
            "query": query,
            "filters": {},
            "size": clamped,
            "status": "no_corpus_wired",
        }

    # Keep ONLY the whitelisted, non-None filter keys — a planner cannot
    # term-filter on an arbitrary field (that would silently match nothing).
    # A non-dict ``filters`` (a mis-emitted string/list) coerces to {} so a
    # bad shape degrades to an unfiltered search, never an AttributeError
    # (mirrors compare_targets' isinstance type-guard on container args).
    raw_filters = filters if isinstance(filters, dict) else {}
    clean: dict[str, Any] = {
        k: v
        for k, v in raw_filters.items()
        if k in CORPUS_FILTER_KEYS and v is not None
    }

    try:
        await store.connect()  # idempotent — no-op if connected
        rows = await store.search(
            index,
            (query or "").strip() or None,
            filters=clean or None,
            size=clamped,
        )
    except Exception as exc:  # noqa: BLE001 — corpus backend surface
        logger.warning(
            "substrate_query_port.search_corpus.failed err=%s", exc,
        )
        return {
            "rows": [],
            "refs": [],
            "count": 0,
            "query": query,
            "filters": clean,
            "size": clamped,
            "error": f"corpus_search_failed: {exc!s}",
        }
    # THE INVARIANT (2026-09-16 review, defect 2): this reader used to
    # return the raw hits — each one a whole ~6 KB _source — with no
    # ``refs`` key at all, so eight lexical hits contributed ZERO citable
    # substrate and the rows themselves overflowed the conversation's 8 KB
    # tool-message bound (5 of 8 dropped by ``_bounded_tool_json``). The
    # projection lifts each hit's ``_id`` (which IS the ``signals.id``) into
    # ``refs`` and trims each row to a citable header + snippet; a hit that
    # cannot yield a UUID ref is dropped with a counted reason rather than
    # counted as a result the planner cannot cite.
    projected = project_corpus_hits(rows)
    projected.update({
        "query": query,
        "filters": clean,
        "size": clamped,
    })
    return projected

async def read_document(
    store: Any, index: str, pool: Any, *, doc_id: str,
) -> dict[str, Any]:
    """Fetch one document's body by id, across every id NAMESPACE the
    planner can be holding (Stage 1; repaired by the 2026-09-16 review).

    The live run called this three times and the trace read ``count 0,
    refs 0`` every time — including on ``3f69f001…``, the interview the
    whole answer rested on. The document WAS there (verified read-only
    against the live index). Three things were wrong and all three are
    fixed here:

    * **no refs** — the result carried no ``refs`` key, so the document the
      model had just read was not citable. It now returns the doc id as a
      substrate ref (the corpus ``_id`` IS the ``signals.id``).
    * **no count** — a successful read rendered as ``count 0``, which reads
      as an empty result to anyone auditing the trace.
    * **unbounded** — it returned the whole ``_source``: raw HTML
      ``raw_body`` PLUS ``archived_text`` PLUS a duplicate ``best_body``.
      Two of the three live calls fetched 52-55 KB docs, which
      ``_bounded_tool_json``'s 8 KB conversation bound collapsed into a
      ``raw_prefix`` string of chopped mid-JSON tag soup. The projection
      now picks ONE body, flattens its markup and declares its own cut.

    NAMESPACES. A planner holds ids from every reader it has called, and
    ``list_findings`` / ``list_situations`` hand out ids that are NOT in
    the corpus. Rather than answer ``not_found`` to a live row, the lookup
    falls back: corpus doc → ``signals`` row (present but not yet indexed,
    or indexed without prose) → the row's canonical twin when the id is a
    DEDUP ALIAS → ``analyst_outputs`` (a finding id). ``origin`` says which
    namespace answered, so the model knows whether it is reading a source
    document or the platform's own synthesis.

    Degrade-not-break: a backend failure is logged and folded into an
    ``error`` field rather than raising into the consult loop.
    """
    ref = citable_ref(doc_id)
    if ref is None:
        return {
            "status": "invalid_doc_id",
            "doc_id": doc_id,
            "refs": [],
            "count": 0,
            "error": (
                "doc_id must be a substrate UUID — pass an id a reader "
                "returned (search_corpus row id, list_findings row id), "
                "not a title or a URL."
            ),
        }

    corpus_error: str | None = None
    bodyless: dict[str, Any] | None = None
    if store is not None:
        try:
            await store.connect()  # idempotent
            src = await store.get(index, ref)
        except Exception as exc:  # noqa: BLE001 — corpus backend surface
            logger.warning(
                "substrate_query_port.read_document.failed err=%s", exc,
            )
            corpus_error = f"read_document_failed: {exc!s}"
            src = None
        if src is not None:
            projected = project_corpus_document(ref, src)
            if projected["count"]:
                return projected
            # Indexed but body-less (a structured-payload signal whose text
            # fields were dropped at index time). Try Postgres, which holds
            # the payload the indexer declined to project — and keep this as
            # the answer if Postgres has nothing better.
            bodyless = projected

    fallback = (
        await _read_document_from_pg(pool, ref) if pool is not None else None
    )
    if fallback is not None:
        return fallback
    if bodyless is not None:
        return bodyless
    if store is None and pool is None:
        # Neither namespace reachable — the honest unavailable shape, never a
        # ``not_found`` that would read as "this document does not exist".
        return {
            "status": "no_corpus_wired", "doc_id": ref, "refs": [], "count": 0,
        }
    if corpus_error is not None:
        return {
            "status": "error", "doc_id": ref, "refs": [], "count": 0,
            "error": corpus_error,
        }
    return {"status": "not_found", "doc_id": ref, "refs": [], "count": 0}


async def _read_document_from_pg(pool: Any, ref: str) -> dict[str, Any] | None:
    """Postgres fallback for :meth:`read_document` — the signal row, its
    canonical twin, or an ``analyst_outputs`` finding. None when the id
    names nothing in any of them."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, payload, source_id, canonical_url, language, "
            "       modality, geo, tags, fetched_at, canonical_signal_id "
            "FROM signals WHERE id = $1::uuid",
            ref,
        )
        if row is not None:
            twin = row["canonical_signal_id"]
            if twin is not None and str(twin) != ref:
                # The id is a dedup ALIAS. The corpus/analytic slice reads
                # the canonical row, so answer with THAT row's body and say
                # so — an alias id must not read as a missing document.
                canonical = await conn.fetchrow(
                    "SELECT id, payload, source_id, canonical_url, "
                    "       language, modality, geo, tags, fetched_at "
                    "FROM signals WHERE id = $1::uuid",
                    str(twin),
                )
                if canonical is not None:
                    out = project_signal_row(canonical, origin="signals_canonical")
                    out["alias_of"] = str(twin)
                    out["requested_doc_id"] = ref
                    return out
            return project_signal_row(row, origin="signals")

        finding = await conn.fetchrow(
            "SELECT id, title, body, kind, target_id, analyst_id, "
            "       confidence, severity, produced_at "
            "FROM analyst_outputs WHERE id = $1::uuid",
            ref,
        )
        if finding is not None:
            return project_finding_row(finding)
    return None


__all__ = [
    "CORPUS_FILTER_KEYS",
    "SEARCH_CORPUS_DEFAULT_SIZE",
    "SEARCH_CORPUS_MAX_SIZE",
    "read_document",
    "search_corpus",
]
