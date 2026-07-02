# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Production :class:`SubstrateQueryPort` impl over Postgres + Qdrant.

Closes the activation gate in
:func:`legba.runtime.analyst_deps_builder._build_consult_on_demand` — that
builder raises :class:`AnalystDepsBuildError` when no
``substrate_query_port`` is supplied because the only in-tree
implementation today is the test stub
``tests/runtime/test_spike_integration._StubSubstrate``.

This module ships the real one.  The :class:`SubstrateQueryPort` Protocol
lives in :mod:`legba.data.analysts.consult_on_demand` (that kind module is
the source of truth for the tool surface — the original four
``search_signals`` / ``query_facts`` / ``inspect_entity`` /
``vector_search`` plus the S4 richer readers ``query_nexuses`` /
``query_hypotheses`` / ``get_timeline`` / ``compare_targets``).  We
satisfy it with direct queries against:

  * the substrate Postgres pool (``signals``, ``facts``,
    ``entity_profiles``, ``entity_profile_versions``,
    ``signal_entity_links`` per migrations 0002 + 0003; the reified
    ``nexuses`` table per migration 0033; the ACH ``hypotheses`` table
    per 0001 + 0038; and ``analyst_outputs`` for the ``compare_targets``
    finding rollup), and
  * the canonical ``legba_signals`` Qdrant collection (BGE-M3 1024-dim
    cosine per :mod:`legba.data.qdrant`) for semantic vector search.

The S4 readers honor the same temporal gates as the originals:
``query_nexuses`` returns only OPEN nexuses (``valid_until IS NULL AND
superseded_by IS NULL``), ``get_timeline``'s fact stream and
``compare_targets``'s fact/nexus counts gate to current rows, and
``get_timeline`` anchors each item on a single timestamp (fact:
``valid_from`` → ``produced_at`` → ``created_at``; signal: ``fetched_at``
→ ``created_at``), skipping any row whose anchor resolves to NULL.

Implementation notes
--------------------

* ``search_signals`` runs a Postgres-native full-text search via
  ``to_tsvector('simple', payload->>'title' || ' ' || payload->>'summary')``
  against ``plainto_tsquery``.  The L-178 design brief mentions BM25 over
  a dedicated full-text engine as the preferred backing, but no such
  engine is built (declared seam — see ``docs/SEAMS.md``), so the
  Postgres FTS path is the honest in-tree implementation and the
  ``category`` argument narrows by ``payload->>'category'`` (the
  source-first re-cut in migration 0024 dropped the scalar
  ``signals.category`` column).  If a dedicated full-text index is ever
  added, this method can switch backings without changing the Protocol
  shape.

* ``vector_search`` queries Qdrant's ``legba_signals`` collection.  The
  caller passes a free-form ``query`` string (per the Protocol).  L-114
  threads the hosted embedding client through this port at bring-up
  (``embedder`` kwarg): when present, the method embeds the query then
  runs the cosine search via ``vector_search_by_embedding``.  When no
  embedder is wired (the embedding service wasn't provisioned) we surface
  ``{"unavailable": True, "reason": "no_embedder_wired"}`` rather than
  fabricate a vector — the same ``unavailable`` shape the test stub uses.

* ``query_facts`` is the attribute-half facts table (per migration 0003
  / DM-2); relationship-half traversals over AGE edges raise
  :class:`NotImplementedError` because the consult kind's whitelist
  doesn't include a graph-walking tool today.  Both ``query_facts`` and
  ``inspect_entity`` gate to **current** facts only —
  ``superseded_by IS NULL AND valid_until IS NULL`` (migration 0032) —
  so a consult never reasons over a replaced or expired assertion.

* ``inspect_entity`` walks ``entity_profiles`` by canonical name
  (case-insensitive), then joins ``entity_profile_versions`` for the
  per-version history, ``signal_entity_links`` for the most recent
  N signal mentions, and ``facts`` (subject = canonical name, current
  rows only) for the entity's live attribute facts.

* The ``scope_predicate`` argument on ``search_signals`` is accepted but
  surfaced as a ``"scope_predicate_applied": False`` flag — applying a
  Starlark predicate over rows would require a per-row evaluator pass
  through :mod:`legba.data.predicates`; that wiring is the L-104 follow-
  up.  Per Lewis's no-stubs rule we report the deferral rather than
  silently filter or return synthesized data.

Integration
-----------

The runtime bootstrap in :func:`legba.runtime.dapr_host.bring_up_production_runtime`
should construct this once after :func:`build_qdrant_client_from_stack_component`
returns, and pass it through to
:func:`legba.runtime.analyst_deps_builder.build_analyst_run_method`::

    substrate_query_port = PostgresQdrantSubstrateQueryPort(
        pg_pool=pg_store.pool,
        qdrant_client=qdrant_client,
        embedder=embedding_service,  # L-114 — free-text vector_search
    )
    ...
    await build_analyst_run_method(
        ad,
        ...,
        substrate_query_port=substrate_query_port,
    )

Heavy imports (``qdrant_client``, ``httpx``) sit inside the methods so
this module stays cheap to import — matches the
:mod:`legba.runtime.qdrant_factory` precedent.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = logging.getLogger(__name__)


__all__ = ["PostgresQdrantSubstrateQueryPort"]


# Cap how many rows any one tool round can return.  The consult kind
# caps individual tool calls at ``limit`` (planner-supplied) but we
# clamp here too so a runaway planner can't ask for 10_000 rows and
# wedge a postgres connection.
_MAX_ROW_LIMIT = 200

# The LIVE assessment producers the journal + consult reflect OVER when
# ``get_assessments`` is called with no explicit ``analyst_id``. Replaces the
# retired ``country_assessor``/``world_assessor`` MONOLITH default: the old
# first-order ``country_assessor`` one-pager no longer produces, so keying the
# journal's reflection surface on it read a DEAD surface. The live conclusion
# chain is the four bounded P2 reasoning UNITS + the P3 per-country COMPOSITION
# (``country_composition``) + the P3-T5 world COMPOSITION (``world_assessor``,
# repointed from the retired monolith to ``meta_findings_synthesizer`` — its
# live head rows ARE compositions). Kept in sync with the unit set in
# scorecard_banding.DIMENSIONS / unit_correctness_scorer._DEFAULT_UNITS and the
# composition set in composition_lineage_sweep._COMPOSITION_ANALYSTS. Region
# compositions join this set when that leg lands.
_ASSESSMENT_PRODUCER_ANALYSTS: tuple[str, ...] = (
    # Compositions (second-order reads — the platform's headline conclusions).
    "country_composition",
    "world_assessor",
    # Bounded P2 units (first-order per-country reads the compositions fuse).
    "leadership_transition",
    "energy_security",
    "escalation",
    "narrative_coordination",
    # S1-T4/T5 units — broad (every desk), fused by country_composition + banded
    # as fixed scorecard dimensions.
    "internal_stability",
    "military_posture",
)

# Default number of recent signal mentions ``inspect_entity`` joins in.
_INSPECT_RECENT_SIGNAL_MENTIONS = 10
# Default number of entity_profile_versions rows surfaced by inspect_entity.
_INSPECT_RECENT_VERSIONS = 5
# Default number of current facts (keyed by subject) surfaced by inspect_entity.
_INSPECT_RECENT_FACTS = 30

# ``get_timeline`` pulls at most this many of each contributing stream
# (current facts, recent signals) before the merge + clamp to the caller's
# ``limit`` — bounds the per-stream scan when the merged limit is large.
_TIMELINE_PER_STREAM_CAP = 200
# DQ-#70/F5 — per-kind floor on the MERGED timeline. Signals are far denser than
# facts/situations, so a pure newest-first clamp buries the sparse-but-important
# kinds. Each kind is guaranteed up to this many of its newest items before the
# remaining budget is filled by overall recency.
_TIMELINE_PER_KIND_FLOOR = 6
# ``compare_targets`` clamps the number of target ids it rolls up in one
# call so a runaway planner can't fan a rollup across the whole catalog.
_COMPARE_MAX_TARGETS = 12
# Recent findings surfaced per target by ``compare_targets``.
_COMPARE_RECENT_FINDINGS = 5

# ------------------------------------------------------------------
# Graph traversal (P5 / #99) — recursive-CTE walks over the OPEN nexus
# graph. The nexus graph is CYCLIC (A→B, B→A, A→C→A are all legal), NOT a
# DAG, so every traversal carries a VISITED-SET guard (the path-so-far is
# accumulated as a text[] of lower(node) names and a candidate next hop is
# rejected if it is already in the path) to make termination unconditional
# independent of ``max_hops``. Every walk is additionally bounded by a hard
# hop cap and a per-query row cap so a dense neighborhood can't explode.
# ------------------------------------------------------------------

# Hard ceiling on traversal depth regardless of the caller's request — a
# 3-hop signed path is the deepest the structural-balance / proxy-chain
# tradecraft reads, and CTE fan-out is exponential in hops.
_GRAPH_MAX_HOPS = 3
# Row cap on the recursive frontier AND on the returned path set — bounds
# the working set a single traversal can materialize.
_GRAPH_MAX_PATHS = 100
# Broker discovery caps how many entities it names on each side of the cut
# and how many brokers it returns.
_BROKER_MAX_CAMP = 25
_BROKER_MAX_RESULTS = 50


class PostgresQdrantSubstrateQueryPort:
    """Production :class:`SubstrateQueryPort` over pg_pool + qdrant_client.

    See module docstring for the per-method backing + deferral notes.
    Constructor is keyword-only so the runtime bootstrap can't accidentally
    swap pool / client at the call site.
    """

    def __init__(
        self,
        *,
        pg_pool: "asyncpg.Pool",
        qdrant_client: Any,
        embedder: Any | None = None,
        signals_collection: str = "legba_signals",
    ) -> None:
        self._pool = pg_pool
        self._qdrant = qdrant_client
        # L-114 embedder-through-port: the hosted embedding client
        # (:class:`legba.runtime.embedding_factory.HostedEmbeddingClient`,
        # ``async def embed(text) -> list[float]``) the host threads in at
        # bring-up. When present, ``vector_search`` embeds the free-text
        # query then runs the Qdrant cosine search via
        # ``vector_search_by_embedding``; when None (the embedding service
        # wasn't provisioned) it honestly reports the ``no_embedder_wired``
        # Protocol shape rather than fabricating a vector (seam #11).
        self._embedder = embedder
        self._signals_collection = signals_collection

    # ------------------------------------------------------------------
    # search_signals
    # ------------------------------------------------------------------

    async def search_signals(
        self,
        *,
        query: str,
        category: str | None = None,
        limit: int = 20,
        scope_predicate: str | None = None,
    ) -> dict[str, Any]:
        """Full-text search over ``signals`` via Postgres ``to_tsvector``.

        Returns rows ranked by ``ts_rank`` against
        ``plainto_tsquery('simple', $1)``.  When ``category`` is provided,
        rows are pre-filtered by ``signals.category``.  When ``query`` is
        empty / whitespace we return an empty result (rather than
        running an unbounded scan ranked at zero).
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        q = (query or "").strip()
        if not q:
            return {
                "rows": [],
                "refs": [],
                "query": query,
                "category": category,
                "scope_predicate_applied": False,
                "backing": "postgres_fts",
                "note": "empty_query",
            }

        sql_parts = [
            "SELECT id, payload->>'title' AS title, "
            "payload->>'category' AS category, canonical_url, fetched_at,",
            "       ts_rank(",
            "         to_tsvector('simple', coalesce(payload->>'title','') || ' ' || ",
            "                     coalesce(payload->>'summary','')),",
            "         plainto_tsquery('simple', $1)",
            "       ) AS rank",
            "FROM signals",
            "WHERE to_tsvector('simple', coalesce(payload->>'title','') || ' ' || ",
            "                  coalesce(payload->>'summary','')) ",
            "      @@ plainto_tsquery('simple', $1)",
        ]
        params: list[Any] = [q]
        if category:
            sql_parts.append(f"AND payload->>'category' = ${len(params) + 1}")
            params.append(category)
        sql_parts.append("ORDER BY rank DESC, fetched_at DESC")
        sql_parts.append(f"LIMIT ${len(params) + 1}")
        params.append(clamped_limit)
        sql = "\n".join(sql_parts)

        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql, *params)

        rows: list[dict[str, Any]] = []
        refs: list[str] = []
        for r in records:
            rid = r["id"]
            refs.append(str(rid))
            rows.append({
                "id": str(rid),
                "title": r["title"],
                "category": r["category"],
                "source_url": r["canonical_url"],
                "produced_at": r["fetched_at"].isoformat()
                    if isinstance(r["fetched_at"], datetime) else None,
                "rank": float(r["rank"]) if r["rank"] is not None else 0.0,
            })

        result = {
            "rows": rows,
            "refs": refs,
            "query": query,
            "category": category,
            "backing": "postgres_fts",
            "scope_predicate_applied": False,
        }
        if scope_predicate:
            # We accept the argument so the kind can pass it through, but
            # we don't evaluate Starlark here yet — surface the deferral
            # instead of silently filtering.  See module docstring.
            result["scope_predicate_note"] = (
                "scope_predicate received but not evaluated — Starlark "
                "row-level evaluation is the L-104 follow-up."
            )
        return result

    # ------------------------------------------------------------------
    # query_facts
    # ------------------------------------------------------------------

    async def query_facts(
        self,
        *,
        subject: str | None = None,
        predicate: str | None = None,
        value: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Search the ``facts`` table by subject / predicate / value.

        Requires at least one of the three filters per the consult kind's
        system prompt.  When all three are None we return a structured
        error so the planner can correct rather than running an unbounded
        scan.  Substring matching via ``ILIKE`` on subject + value;
        predicate is an exact match (predicates are a closed vocabulary
        per ``predicates.py``).

        Only **current** facts are returned: the bitemporal columns added
        in migration 0032 (``superseded_by`` / ``valid_until``) gate the
        result to ``superseded_by IS NULL AND valid_until IS NULL`` so a
        consult never reasons over a fact that a later assertion has
        replaced or that has explicitly expired.  This is the same "open
        row" predicate the unique-triple index scopes to.
        """
        if subject is None and predicate is None and value is None:
            return {
                "rows": [],
                "refs": [],
                "error": (
                    "query_facts requires at least one of subject, "
                    "predicate, or value"
                ),
            }
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))

        # Current-facts gate (Piece-B follow-up): never surface superseded
        # or expired rows.  See migration 0032 — NULL on both columns is the
        # canonical "open / live" fact.
        clauses: list[str] = [
            "superseded_by IS NULL",
            "valid_until IS NULL",
        ]
        params: list[Any] = []
        if subject is not None:
            params.append(f"%{subject}%")
            clauses.append(f"subject ILIKE ${len(params)}")
        if predicate is not None:
            params.append(predicate)
            clauses.append(f"predicate = ${len(params)}")
        if value is not None:
            params.append(f"%{value}%")
            clauses.append(f"value ILIKE ${len(params)}")
        where = " AND ".join(clauses)
        params.append(clamped_limit)
        sql = (
            # source_type rides on every row (F1) so a reader — the consult /
            # deep_consult LLM, the agency read tools, the UI — can tell an
            # operator-vetted seed/curated fact from an automated ingestion/agent
            # extraction and discount the latter. This surface LEGITIMATELY
            # serves ingestion data (unlike the grounding preamble, which gates
            # it out), so it LABELS rather than drops.
            "SELECT id, subject, predicate, value, confidence, source_type, "
            "       valid_from, produced_at, target_id, analyst_id "
            "FROM facts "
            f"WHERE {where} "
            "ORDER BY produced_at DESC "
            f"LIMIT ${len(params)}"
        )

        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql, *params)

        rows: list[dict[str, Any]] = []
        refs: list[str] = []
        for r in records:
            rid = r["id"]
            refs.append(str(rid))
            rows.append({
                "id": str(rid),
                "subject": r["subject"],
                "predicate": r["predicate"],
                "value": r["value"],
                "confidence": float(r["confidence"])
                    if r["confidence"] is not None else None,
                "source_type": r["source_type"],
                "valid_from": r["valid_from"].isoformat()
                    if isinstance(r["valid_from"], datetime) else None,
                "produced_at": r["produced_at"].isoformat()
                    if isinstance(r["produced_at"], datetime) else None,
                "target_id": r["target_id"],
                "analyst_id": r["analyst_id"],
            })

        return {
            "rows": rows,
            "refs": refs,
            "filters": {
                "subject": subject,
                "predicate": predicate,
                "value": value,
            },
        }

    # ------------------------------------------------------------------
    # inspect_entity
    # ------------------------------------------------------------------

    async def inspect_entity(self, *, name: str) -> dict[str, Any]:
        """Return the latest profile + recent versions + recent mentions.

        Matches on ``LOWER(canonical_name) = LOWER($1)`` per the unique
        index in migration 0002.  Returns an empty-but-shaped result when
        no entity is found (rather than raising) so the planner can fall
        back to ``search_signals`` / ``query_facts`` without a crash.
        """
        n = (name or "").strip()
        if not n:
            return {
                "entity": name,
                "found": False,
                "facts": [],
                "versions": [],
                "recent_signal_mentions": [],
                "refs": [],
                "error": "name must be non-empty",
            }

        async with self._pool.acquire() as conn:
            profile = await conn.fetchrow(
                """
                SELECT id, canonical_name, entity_type, entity_class,
                       version, completeness_score, last_event_link_at,
                       last_verified_at, geo_country, geo_region,
                       produced_at, analyst_id, target_id
                FROM entity_profiles
                WHERE LOWER(canonical_name) = LOWER($1)
                """,
                n,
            )
            if profile is None:
                return {
                    "entity": name,
                    "found": False,
                    "facts": [],
                    "versions": [],
                    "recent_signal_mentions": [],
                    "refs": [],
                }

            entity_id = profile["id"]
            version_rows = await conn.fetch(
                """
                SELECT id, version, cycle_number, analyst_id, created_at
                FROM entity_profile_versions
                WHERE entity_id = $1
                ORDER BY version DESC, created_at DESC
                LIMIT $2
                """,
                entity_id,
                _INSPECT_RECENT_VERSIONS,
            )
            mention_rows = await conn.fetch(
                """
                SELECT sel.signal_id, sel.role, sel.confidence, sel.created_at,
                       s.payload->>'title' AS title,
                       s.payload->>'category' AS category,
                       s.fetched_at AS signal_produced_at
                FROM signal_entity_links sel
                LEFT JOIN signals s ON s.id = sel.signal_id
                WHERE sel.entity_id = $1
                ORDER BY sel.created_at DESC
                LIMIT $2
                """,
                entity_id,
                _INSPECT_RECENT_SIGNAL_MENTIONS,
            )
            # Current facts about this entity, keyed by subject = canonical
            # name (the same enumerate-via-subject convention the prior
            # facts_note pointed callers at).  Gated to OPEN rows only —
            # superseded_by IS NULL AND valid_until IS NULL (migration 0032)
            # — so inspect_entity never surfaces a replaced/expired fact.
            fact_rows = await conn.fetch(
                """
                SELECT id, subject, predicate, value, confidence,
                       valid_from, produced_at
                FROM facts
                WHERE LOWER(subject) = LOWER($1)
                  AND superseded_by IS NULL
                  AND valid_until IS NULL
                ORDER BY produced_at DESC
                LIMIT $2
                """,
                profile["canonical_name"],
                _INSPECT_RECENT_FACTS,
            )

        refs: list[str] = [str(entity_id)]
        versions: list[dict[str, Any]] = []
        for v in version_rows:
            refs.append(str(v["id"]))
            versions.append({
                "id": str(v["id"]),
                "version": v["version"],
                "cycle_number": v["cycle_number"],
                "analyst_id": v["analyst_id"],
                "created_at": v["created_at"].isoformat()
                    if isinstance(v["created_at"], datetime) else None,
            })

        facts: list[dict[str, Any]] = []
        for f in fact_rows:
            fid = f["id"]
            refs.append(str(fid))
            facts.append({
                "id": str(fid),
                "subject": f["subject"],
                "predicate": f["predicate"],
                "value": f["value"],
                "confidence": float(f["confidence"])
                    if f["confidence"] is not None else None,
                "valid_from": f["valid_from"].isoformat()
                    if isinstance(f["valid_from"], datetime) else None,
                "produced_at": f["produced_at"].isoformat()
                    if isinstance(f["produced_at"], datetime) else None,
            })

        mentions: list[dict[str, Any]] = []
        for m in mention_rows:
            sid = m["signal_id"]
            refs.append(str(sid))
            mentions.append({
                "signal_id": str(sid),
                "role": m["role"],
                "confidence": float(m["confidence"])
                    if m["confidence"] is not None else None,
                "linked_at": m["created_at"].isoformat()
                    if isinstance(m["created_at"], datetime) else None,
                "title": m["title"],
                "category": m["category"],
                "signal_produced_at": m["signal_produced_at"].isoformat()
                    if isinstance(m["signal_produced_at"], datetime) else None,
            })

        return {
            "entity": name,
            "found": True,
            "profile": {
                "id": str(entity_id),
                "canonical_name": profile["canonical_name"],
                "entity_type": profile["entity_type"],
                "entity_class": profile["entity_class"],
                "version": profile["version"],
                "completeness_score": float(profile["completeness_score"])
                    if profile["completeness_score"] is not None else None,
                "last_event_link_at": profile["last_event_link_at"].isoformat()
                    if isinstance(profile["last_event_link_at"], datetime)
                    else None,
                "last_verified_at": profile["last_verified_at"].isoformat()
                    if isinstance(profile["last_verified_at"], datetime)
                    else None,
                "geo_country": profile["geo_country"],
                "geo_region": profile["geo_region"],
                "produced_at": profile["produced_at"].isoformat()
                    if isinstance(profile["produced_at"], datetime) else None,
                "analyst_id": profile["analyst_id"],
                "target_id": profile["target_id"],
            },
            "facts": facts,
            "facts_note": (
                "current facts keyed by subject = canonical_name "
                "(superseded/expired rows excluded); for substring or "
                "predicate-scoped enumeration call query_facts(subject="
                f"{profile['canonical_name']!r})"
            ),
            "versions": versions,
            "recent_signal_mentions": mentions,
            "refs": refs,
        }

    # ------------------------------------------------------------------
    # vector_search
    # ------------------------------------------------------------------

    async def vector_search(
        self,
        *,
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Semantic similarity search over ``legba_signals`` in Qdrant.

        The consult Protocol passes a free-form ``query`` string here.
        L-114 threads the hosted embedding client through this port at
        bring-up: when an ``embedder`` is present we embed the query then
        run the Qdrant cosine search via :meth:`vector_search_by_embedding`.
        When no embedder is wired (the embedding service wasn't
        provisioned) we report ``unavailable=True`` (matching the
        Protocol's documented shape) rather than fabricating a vector or
        falling back to a different backing (seam #11).

        The collection-level filter on ``target_id`` is left for the
        per-target collection-naming follow-up (per
        :func:`legba.data.qdrant.QdrantStore.ensure_target_collection`);
        today the single ``legba_signals`` collection is queried.
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))

        # No embedder threaded through the port — surface the honest
        # ``unavailable`` shape rather than synthesizing an embedding. The
        # test stub reports the same shape so the consult kind already
        # handles this path (seam #11 fallback).
        if self._embedder is None:
            return {
                "rows": [],
                "refs": [],
                "query": query,
                "limit": clamped_limit,
                "collection": self._signals_collection,
                "unavailable": True,
                "reason": (
                    "no_embedder_wired — vector_search requires an embedding "
                    "model surfaced through this port; wire an embedding "
                    "service at bring-up (embed.primary.openai_compat)"
                ),
            }

        # Empty query — mirror ``search_signals``: skip the embed round-trip
        # and return an empty result rather than embedding whitespace.
        q = (query or "").strip()
        if not q:
            return {
                "rows": [],
                "refs": [],
                "query": query,
                "limit": clamped_limit,
                "collection": self._signals_collection,
                "backing": "qdrant_cosine",
                "note": "empty_query",
            }

        # Embed the free-text query, then delegate the Qdrant cosine search
        # to the shared by-embedding helper. An embed failure degrades to
        # the honest ``unavailable`` shape (never a fabricated vector).
        try:
            vec = await self._embedder.embed(q)
        except Exception as exc:  # noqa: BLE001 — embed backend surface
            logger.warning(
                "substrate_query_port.vector_search.embed_failed err=%s", exc,
            )
            return {
                "rows": [],
                "refs": [],
                "query": query,
                "limit": clamped_limit,
                "collection": self._signals_collection,
                "unavailable": True,
                "reason": f"embed_failed: {exc!s}",
            }

        result = await self.vector_search_by_embedding(
            query_embedding=vec, limit=clamped_limit,
        )
        # Carry the caller's original free-text query through + tag the
        # backing so the consult trace shows the semantic path ran.
        result["query"] = query
        result["backing"] = "qdrant_cosine"
        return result

    # ------------------------------------------------------------------
    # query_nexuses (S4-T6)
    # ------------------------------------------------------------------

    async def query_nexuses(
        self,
        *,
        subject: str | None = None,
        obj: str | None = None,
        rel_type: str | None = None,
        polarity: int | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Search the reified ``nexuses`` table (migration 0033).

        A nexus is the first-class reified relationship — A → (optional
        typed intermediary) → B — carrying a canonical POLARITY sign,
        ``rel_type``, intent, channel, and temporal bounds.  Filters:
        ``subject`` / ``obj`` substring-match (``ILIKE`` on ``subject`` /
        ``object``), ``rel_type`` exact (the predicate vocabulary), and
        ``polarity`` exact (+1 supportive / -1 antagonistic / 0
        neutral-dual-use).  All filters are optional; with none supplied
        the most-recent OPEN nexuses are returned.

        Only **open** nexuses are returned — the same gate the
        structural-balance / proxy-chain consumers use:
        ``valid_until IS NULL AND superseded_by IS NULL`` (migration
        0033).  A consult never reasons over a superseded or expired
        relationship.
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))

        # Open-nexus gate — "what holds now" is the single open row.
        clauses: list[str] = [
            "valid_until IS NULL",
            "superseded_by IS NULL",
        ]
        params: list[Any] = []
        if subject is not None:
            params.append(f"%{subject}%")
            clauses.append(f"subject ILIKE ${len(params)}")
        if obj is not None:
            params.append(f"%{obj}%")
            clauses.append(f"object ILIKE ${len(params)}")
        if rel_type is not None:
            params.append(rel_type)
            clauses.append(f"rel_type = ${len(params)}")
        if polarity is not None:
            params.append(int(polarity))
            clauses.append(f"polarity = ${len(params)}")
        where = " AND ".join(clauses)
        params.append(clamped_limit)
        sql = (
            # source_type rides on every row (F1) — same label-not-drop rationale
            # as query_facts. (Live nexuses carry no 'ingestion' lane, only
            # seed/agent, so this mainly distinguishes seed ground truth from the
            # reified/promoted agent lane — still worth surfacing for symmetry.)
            "SELECT id, subject, intermediary, object, rel_type, label, "
            "       polarity, intent, channel, confidence, source_type, "
            "       valid_from, produced_at, target_id, analyst_id "
            "FROM nexuses "
            f"WHERE {where} "
            "ORDER BY produced_at DESC "
            f"LIMIT ${len(params)}"
        )

        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql, *params)

        rows: list[dict[str, Any]] = []
        refs: list[str] = []
        for r in records:
            rid = r["id"]
            refs.append(str(rid))
            rows.append({
                "id": str(rid),
                "subject": r["subject"],
                "intermediary": r["intermediary"],
                "object": r["object"],
                "rel_type": r["rel_type"],
                "label": r["label"],
                "polarity": int(r["polarity"]) if r["polarity"] is not None else None,
                "intent": r["intent"],
                "channel": r["channel"],
                "confidence": float(r["confidence"])
                    if r["confidence"] is not None else None,
                "source_type": r["source_type"],
                "valid_from": r["valid_from"].isoformat()
                    if isinstance(r["valid_from"], datetime) else None,
                "produced_at": r["produced_at"].isoformat()
                    if isinstance(r["produced_at"], datetime) else None,
                "target_id": r["target_id"],
                "analyst_id": r["analyst_id"],
            })

        return {
            "rows": rows,
            "refs": refs,
            "filters": {
                "subject": subject,
                "object": obj,
                "rel_type": rel_type,
                "polarity": polarity,
            },
        }

    # ------------------------------------------------------------------
    # query_hypotheses (S4-T6)
    # ------------------------------------------------------------------

    async def query_hypotheses(
        self,
        *,
        target_id: str | None = None,
        status: str | None = None,
        situation_id: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Search the ACH ``hypotheses`` table (migration 0001 + 0038).

        A hypothesis is a competing-hypothesis row: ``thesis`` vs
        ``counter_thesis``, with a diagnostic ``evidence_balance`` and a
        ``status`` (``active`` / ``confirmed`` / ``refuted``) that the
        competing_hypotheses kind auto-transitions past ±K.  Filters
        (all optional): ``target_id`` exact, ``status`` exact,
        ``situation_id`` exact (the situation the hypothesis hangs off,
        per ``hypotheses.situation_id``).  Hypotheses are not bitemporal —
        there is no open/superseded gate here — so the most-recent rows
        matching the filters are returned, ordered by ``produced_at``.

        The EXOGENOUS resolution columns (migration 0038 —
        ``resolved_outcome`` / ``resolved_at`` / ``resolved_by``) are
        surfaced too so a consult can distinguish a hypothesis the world
        subsequently resolved from one still scored only on self-consistent
        evidence balance.
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))

        clauses: list[str] = []
        params: list[Any] = []
        if target_id is not None:
            params.append(target_id)
            clauses.append(f"target_id = ${len(params)}")
        if status is not None:
            params.append(status)
            clauses.append(f"status = ${len(params)}")
        if situation_id is not None:
            params.append(situation_id)
            clauses.append(f"situation_id = ${len(params)}::uuid")
        where = (" AND ".join(clauses)) if clauses else "TRUE"
        params.append(clamped_limit)
        sql = (
            "SELECT id, situation_id, thesis, counter_thesis, "
            "       evidence_balance, status, "
            "       array_length(supporting_signals, 1) AS supporting_count, "
            "       array_length(refuting_signals, 1) AS refuting_count, "
            "       resolved_outcome, resolved_at, resolved_by, "
            "       target_id, analyst_id, produced_at "
            "FROM hypotheses "
            f"WHERE {where} "
            "ORDER BY produced_at DESC "
            f"LIMIT ${len(params)}"
        )

        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql, *params)

        rows: list[dict[str, Any]] = []
        refs: list[str] = []
        for r in records:
            rid = r["id"]
            refs.append(str(rid))
            rows.append({
                "id": str(rid),
                "situation_id": str(r["situation_id"])
                    if r["situation_id"] is not None else None,
                "thesis": r["thesis"],
                "counter_thesis": r["counter_thesis"],
                "evidence_balance": r["evidence_balance"],
                "status": r["status"],
                "supporting_count": r["supporting_count"] or 0,
                "refuting_count": r["refuting_count"] or 0,
                "resolved_outcome": r["resolved_outcome"],
                "resolved_at": r["resolved_at"].isoformat()
                    if isinstance(r["resolved_at"], datetime) else None,
                "resolved_by": r["resolved_by"],
                "target_id": r["target_id"],
                "analyst_id": r["analyst_id"],
                "produced_at": r["produced_at"].isoformat()
                    if isinstance(r["produced_at"], datetime) else None,
            })

        return {
            "rows": rows,
            "refs": refs,
            "filters": {
                "target_id": target_id,
                "status": status,
                "situation_id": situation_id,
            },
        }

    # ------------------------------------------------------------------
    # FINISHED INTELLIGENCE readers (the platform's OWN analytical products —
    # findings / situations / predictions; analysis-derived, source_type 'agent').
    # Wired as consult/GATHER tools so the agent can build on prior analysis
    # instead of re-deriving from raw signals. See planning/CONSULT_PALETTE_*.
    # ------------------------------------------------------------------

    async def list_findings(
        self,
        *,
        target_id: str | None = None,
        analyst_id: str | None = None,
        severity: str | None = None,
        since_hours: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """The platform's own recent FINDINGS, with the critic-folded
        ``effective_confidence = min(confidence, critic_score)``.

        Reuses the substrate-reads ``list_findings`` shape (the finding<->critique
        LEFT JOIN LATERAL that surfaces the critic's ``overall_score``), dropping
        the FastAPI cursor/auth layer. Findings are analysis-derived (the
        platform's own synthesis), NOT raw signals — weigh accordingly.
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        clauses: list[str] = ["f.kind = 'finding'"]
        params: list[Any] = []
        if target_id is not None:
            params.append(target_id)
            clauses.append(f"f.target_id = ${len(params)}")
        if analyst_id is not None:
            params.append(analyst_id)
            clauses.append(f"f.analyst_id = ${len(params)}")
        if severity is not None:
            params.append(severity)
            clauses.append(f"f.severity = ${len(params)}")
        if since_hours is not None:
            params.append(datetime.now(timezone.utc) - timedelta(hours=int(since_hours)))
            clauses.append(f"f.produced_at >= ${len(params)}")
        params.append(clamped_limit)
        sql = (
            "SELECT f.id, f.title, f.body, f.confidence, f.severity, "
            "       f.target_id, f.analyst_id, f.produced_at, "
            "       c.critic_score AS critic_score "
            "FROM analyst_outputs f "
            "LEFT JOIN LATERAL ( "
            "  SELECT (cr.data->>'overall_score')::real AS critic_score "
            "  FROM analyst_outputs cr "
            "  WHERE cr.kind = 'critique' "
            "    AND cr.data->>'analyzed_output_id' = f.id::text "
            "    AND cr.data->>'overall_score' IS NOT NULL "
            "  ORDER BY cr.produced_at DESC, cr.id DESC LIMIT 1 "
            ") c ON TRUE "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY f.produced_at DESC, f.id DESC "
            f"LIMIT ${len(params)}"
        )
        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql, *params)

        rows: list[dict[str, Any]] = []
        refs: list[str] = []
        for r in records:
            refs.append(str(r["id"]))
            confidence = float(r["confidence"]) if r["confidence"] is not None else None
            cs = r["critic_score"]
            critic_score = float(cs) if cs is not None else None
            effective = (
                min(confidence, critic_score)
                if (confidence is not None and critic_score is not None)
                else confidence
            )
            rows.append({
                "id": str(r["id"]),
                "title": r["title"],
                "body": r["body"],
                "confidence": confidence,
                "critic_score": critic_score,
                "effective_confidence": effective,
                "severity": r["severity"],
                "target_id": r["target_id"],
                "analyst_id": r["analyst_id"],
                "produced_at": r["produced_at"].isoformat()
                    if isinstance(r["produced_at"], datetime) else None,
            })
        return {"rows": rows, "refs": refs, "count": len(rows)}

    async def list_situations(
        self,
        *,
        status: str | None = None,
        target_id: str | None = None,
        since_hours: int | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """First-class ``situations`` (the platform's clustered ongoing frames).

        Analysis-derived (clustered from findings), not operator-vetted ground
        truth. Pass a returned ``situation_id`` to ``query_hypotheses`` to pull
        the ACH rows hanging off a situation.
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            params.append(status)
            clauses.append(f"status = ${len(params)}")
        if target_id is not None:
            params.append(target_id)
            clauses.append(f"target_id = ${len(params)}")
        if since_hours is not None:
            params.append(datetime.now(timezone.utc) - timedelta(hours=int(since_hours)))
            # Recency = last ACTIVITY, not first creation. produced_at is frozen
            # at first-cluster (the upsert never bumps it), so filtering it would
            # silently drop a weeks-old frame that just took a fresh member.
            # updated_at is refreshed on every re-cluster (NOT NULL, now() default).
            clauses.append(f"updated_at >= ${len(params)}")
        where = (" AND ".join(clauses)) if clauses else "TRUE"
        params.append(clamped_limit)
        sql = (
            "SELECT id, name, status, category, "
            "       event_count, intensity_score, "
            "       target_id, analyst_id, produced_at, updated_at "
            "FROM situations "
            f"WHERE {where} "
            "ORDER BY updated_at DESC, produced_at DESC "
            f"LIMIT ${len(params)}"
        )
        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql, *params)

        rows: list[dict[str, Any]] = []
        refs: list[str] = []
        for r in records:
            refs.append(str(r["id"]))
            rows.append({
                "id": str(r["id"]),
                "name": r["name"],
                "status": r["status"],
                "category": r["category"],
                "event_count": r["event_count"],
                "intensity_score": r["intensity_score"],
                "target_id": r["target_id"],
                "analyst_id": r["analyst_id"],
                "produced_at": r["produced_at"].isoformat()
                    if isinstance(r["produced_at"], datetime) else None,
                "updated_at": r["updated_at"].isoformat()
                    if isinstance(r["updated_at"], datetime) else None,
            })
        return {"rows": rows, "refs": refs, "count": len(rows)}

    async def query_predictions(
        self,
        *,
        target_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """The platform's event-volume forecasts (analyst_outputs kind='prediction').

        The forecast fields ride in ``data->'prediction'`` (PredictionPayload,
        extra='allow' so the predictor's point_estimate / ci_* / horizon_days /
        method / narrative are carried there). ``forecast_method`` (the writer's
        ``method`` extra) of ``naive_mean`` ⇒ no trend could be fit (weak prior);
        ``auto_arima`` ⇒ a model was fitted.
        Title/body columns are empty on prediction rows by design — read the blob.
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        clauses: list[str] = ["kind = 'prediction'"]
        params: list[Any] = []
        if target_id is not None:
            params.append(target_id)
            clauses.append(f"target_id = ${len(params)}")
        if status is not None:
            params.append(status)
            # The resolver (calibration_tracking) writes the lifecycle status to
            # the TOP LEVEL of data (data->>'status' via jsonb ``||``); the
            # predictor's initial 'open' lives nested under data->'prediction'.
            # Match the resolver's convention first, fall back to the nested
            # initial value so a never-resolved 'open' row still filters.
            clauses.append(
                f"COALESCE(data->>'status', data->'prediction'->>'status') "
                f"= ${len(params)}"
            )
        params.append(clamped_limit)
        sql = (
            "SELECT id, target_id, analyst_id, produced_at, data "
            "FROM analyst_outputs "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY produced_at DESC, id DESC "
            f"LIMIT ${len(params)}"
        )
        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql, *params)

        rows: list[dict[str, Any]] = []
        refs: list[str] = []
        for r in records:
            refs.append(str(r["id"]))
            raw = r["data"]
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
            pred = data.get("prediction") if isinstance(data, dict) else None
            if not isinstance(pred, dict):
                pred = data if isinstance(data, dict) else {}
            rows.append({
                "id": str(r["id"]),
                "target_id": r["target_id"],
                "analyst_id": r["analyst_id"],
                "produced_at": r["produced_at"].isoformat()
                    if isinstance(r["produced_at"], datetime) else None,
                # The predictor stashes the numerics as PredictionPayload
                # extras (predictor.py): ``ci_lower`` / ``ci_upper`` / ``method``
                # (NOT ci_low/ci_high/forecast_method). Read the writer's keys
                # first, accept the alt spellings as a fallback so a future
                # rename can't silently null the CI out.
                "point_estimate": pred.get("point_estimate"),
                "ci_low": pred.get("ci_lower", pred.get("ci_low")),
                "ci_high": pred.get("ci_upper", pred.get("ci_high")),
                "ci_level": pred.get("ci_level"),
                "horizon_days": pred.get("horizon_days"),
                "forecast_method": pred.get("method") or pred.get("forecast_method"),
                "narrative": pred.get("narrative") or pred.get("hypothesis"),
                # Lifecycle status + outcome are written to the TOP LEVEL of
                # data by the resolver; the nested copy is the stale initial
                # 'open'. Read top-level first so a graded prediction reports
                # its true resolved/refuted state, not 'open'.
                "status": (
                    data.get("status") if isinstance(data, dict) else None
                ) or pred.get("status"),
                "resolved_outcome": (
                    data.get("resolved_outcome") if isinstance(data, dict) else None
                ),
            })
        return {"rows": rows, "refs": refs, "count": len(rows)}

    # ------------------------------------------------------------------
    # NAVIGATION readers (resolve scope — targets / source coverage).
    # ------------------------------------------------------------------

    async def list_targets(self, *, active_only: bool = True) -> dict[str, Any]:
        """The monitored targets + their ids (e.g. country_g20_ir), geo, and tags.

        Lets a freeform consult resolve a place/topic to a valid target_id before
        calling query_hypotheses / compare_targets / list_findings.
        """
        clauses: list[str] = ["is_head = TRUE"]
        if active_only:
            clauses.append("state = 'active'")
        sql = (
            "SELECT descriptor_id, body FROM target_descriptors "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY descriptor_id"
        )
        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql)

        rows: list[dict[str, Any]] = []
        for r in records:
            raw = r["body"]
            body = json.loads(raw) if isinstance(raw, str) else (raw or {})
            ident = body.get("identity") or {}
            scope = body.get("scope") or {}
            rows.append({
                "target_id": r["descriptor_id"],
                "name": ident.get("name"),
                "geo": scope.get("geo") or [],
                "tags": scope.get("tags") or body.get("tags") or [],
            })
        return {"rows": rows, "refs": [], "count": len(rows)}

    async def list_sources(
        self,
        *,
        active_only: bool = True,
        silent_only: bool = False,
        silent_hours: int = 48,
    ) -> dict[str, Any]:
        """The ingest sources and their freshness/coverage.

        Joins each head source descriptor to its most-recent signal time and its
        most-recent poll outcome (``source_poll_outcomes``: 'empty'|'error' rollup,
        migration 0046). Use to qualify a 'no signal on X' answer — no coverage vs
        a quiet feed. ``silent_only`` filters to sources silent > ``silent_hours``.
        """
        clauses: list[str] = ["s.is_head = TRUE"]
        if active_only:
            clauses.append("s.state = 'active'")
        sql = (
            "SELECT s.descriptor_id AS source_id, "
            "       s.body->'identity'->>'name' AS name, s.state, "
            "       sig.last_signal_at, po.outcome AS last_poll_outcome, "
            "       po.occurred_at AS last_poll_at "
            "FROM source_descriptors s "
            "LEFT JOIN LATERAL ( "
            "  SELECT max(fetched_at) AS last_signal_at FROM signals WHERE source_id = s.descriptor_id "
            ") sig ON TRUE "
            "LEFT JOIN LATERAL ( "
            "  SELECT outcome, occurred_at FROM source_poll_outcomes "
            "  WHERE source_id = s.descriptor_id ORDER BY occurred_at DESC LIMIT 1 "
            ") po ON TRUE "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY sig.last_signal_at ASC NULLS FIRST"
        )
        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql)

        now = datetime.now(timezone.utc)
        rows: list[dict[str, Any]] = []
        for r in records:
            lsa = r["last_signal_at"]
            silent_h: float | None = None
            if isinstance(lsa, datetime):
                silent_h = round((now - lsa).total_seconds() / 3600.0, 1)
            if silent_only and (silent_h is None or silent_h < silent_hours):
                continue
            rows.append({
                "source_id": r["source_id"],
                "name": r["name"],
                "state": r["state"],
                "last_signal_at": lsa.isoformat() if isinstance(lsa, datetime) else None,
                "silent_hours": silent_h,
                "last_poll_outcome": r["last_poll_outcome"],
            })
        return {"rows": rows, "refs": [], "count": len(rows)}

    # ------------------------------------------------------------------
    # get_timeline (S4-T6)
    # ------------------------------------------------------------------

    async def get_timeline(
        self,
        *,
        subject: str,
        limit: int = 40,
    ) -> dict[str, Any]:
        """Time-ordered merge of current facts + recent signals on a subject.

        Builds one chronological view of what the substrate holds about a
        subject by merging two streams:

          * **facts** — current rows (``superseded_by IS NULL AND
            valid_until IS NULL``, migration 0032) whose ``subject``
            substring-matches the argument; and
          * **signals** — recent signals whose title/summary FTS-matches
            the subject (the same Postgres ``to_tsvector`` backing
            ``search_signals`` uses).

        Each item carries a single temporal anchor: a fact anchors on
        ``valid_from`` and falls back to ``produced_at`` then
        ``created_at``; a signal anchors on ``fetched_at`` then
        ``created_at``.  Items whose anchor resolves to NULL are skipped
        (per the get_timeline temporal-anchor rule) — an item with no
        usable timestamp can't be placed on a timeline.  The merged list
        is sorted newest-first and clamped to ``limit``.
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        s = (subject or "").strip()
        if not s:
            return {
                "subject": subject,
                "items": [],
                "refs": [],
                "error": "subject must be non-empty",
            }

        async with self._pool.acquire() as conn:
            fact_rows = await conn.fetch(
                """
                SELECT id, subject, predicate, value, confidence,
                       valid_from, produced_at, created_at
                FROM facts
                WHERE subject ILIKE $1
                  AND superseded_by IS NULL
                  AND valid_until IS NULL
                ORDER BY COALESCE(valid_from, produced_at, created_at) DESC
                LIMIT $2
                """,
                f"%{s}%",
                _TIMELINE_PER_STREAM_CAP,
            )
            signal_rows = await conn.fetch(
                """
                SELECT id, payload->>'title' AS title,
                       payload->>'category' AS category,
                       canonical_url, fetched_at, created_at
                FROM signals
                WHERE to_tsvector('simple',
                          coalesce(payload->>'title','') || ' ' ||
                          coalesce(payload->>'summary',''))
                      @@ plainto_tsquery('simple', $1)
                ORDER BY COALESCE(fetched_at, created_at) DESC
                LIMIT $2
                """,
                s,
                _TIMELINE_PER_STREAM_CAP,
            )
            # Situation FRAMES on the subject (5b/5c — situations are the
            # persistent-frame substitute for an events table; see DATA_MODEL).
            # A frame is a span [valid_from, valid_until); it anchors on
            # valid_from so the timeline shows when the situation began alongside
            # the facts + signals. Closed (historical) frames are included — they
            # ARE the "events come and go" history.
            situation_rows = await conn.fetch(
                """
                SELECT id, name, status, intensity_score,
                       valid_from, valid_until, produced_at, created_at
                FROM situations
                WHERE name ILIKE $1
                  AND superseded_by IS NULL
                ORDER BY COALESCE(valid_from, produced_at, created_at) DESC
                LIMIT $2
                """,
                f"%{s}%",
                _TIMELINE_PER_STREAM_CAP,
            )

        # Merge into one stream keyed on a single temporal anchor.  Skip
        # any row whose anchor is NULL — it can't be placed in time.
        merged: list[tuple[datetime, dict[str, Any]]] = []
        refs: list[str] = []
        for r in fact_rows:
            anchor = r["valid_from"] or r["produced_at"] or r["created_at"]
            if not isinstance(anchor, datetime):
                continue
            fid = r["id"]
            refs.append(str(fid))
            merged.append((anchor, {
                "kind": "fact",
                "id": str(fid),
                "at": anchor.isoformat(),
                "subject": r["subject"],
                "predicate": r["predicate"],
                "value": r["value"],
                "confidence": float(r["confidence"])
                    if r["confidence"] is not None else None,
            }))
        for r in signal_rows:
            anchor = r["fetched_at"] or r["created_at"]
            if not isinstance(anchor, datetime):
                continue
            sid = r["id"]
            refs.append(str(sid))
            merged.append((anchor, {
                "kind": "signal",
                "id": str(sid),
                "at": anchor.isoformat(),
                "title": r["title"],
                "category": r["category"],
                "source_url": r["canonical_url"],
            }))
        for r in situation_rows:
            anchor = r["valid_from"] or r["produced_at"] or r["created_at"]
            if not isinstance(anchor, datetime):
                continue
            uid = r["id"]
            refs.append(str(uid))
            until = r["valid_until"]
            merged.append((anchor, {
                "kind": "situation",
                "id": str(uid),
                "at": anchor.isoformat(),
                "name": r["name"],
                "status": r["status"],
                "intensity_score": float(r["intensity_score"])
                    if r["intensity_score"] is not None else None,
                # The span end — None while the frame is still open/ongoing.
                "until": until.isoformat() if isinstance(until, datetime) else None,
            }))

        merged.sort(key=lambda pair: pair[0], reverse=True)  # newest-first
        # DQ-#70/F5 — per-kind floor: guarantee each kind up to
        # ``_TIMELINE_PER_KIND_FLOOR`` of its NEWEST items (round-robin, so a
        # tight budget is shared fairly), then fill the remaining slots by
        # overall recency. Without this, a dense signal stream clamps the whole
        # window to signals and the sparse facts/situations vanish.
        by_kind: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
        for pair in merged:
            by_kind.setdefault(pair[1]["kind"], []).append(pair)
        selected: list[tuple[datetime, dict[str, Any]]] = []
        chosen: set[str] = set()
        for rank in range(_TIMELINE_PER_KIND_FLOOR):
            for rows in by_kind.values():
                if rank < len(rows) and len(selected) < clamped_limit:
                    pair = rows[rank]
                    if pair[1]["id"] not in chosen:
                        selected.append(pair)
                        chosen.add(pair[1]["id"])
        for pair in merged:  # fill the rest by overall recency
            if len(selected) >= clamped_limit:
                break
            if pair[1]["id"] not in chosen:
                selected.append(pair)
                chosen.add(pair[1]["id"])
        selected.sort(key=lambda pair: pair[0], reverse=True)
        items = [item for _, item in selected]

        return {
            "subject": subject,
            "items": items,
            "refs": [item["id"] for item in items],
            "counts": {
                "facts": sum(1 for i in items if i["kind"] == "fact"),
                "signals": sum(1 for i in items if i["kind"] == "signal"),
                "situations": sum(1 for i in items if i["kind"] == "situation"),
            },
        }

    # ------------------------------------------------------------------
    # compare_targets (S4-T6)
    # ------------------------------------------------------------------

    async def compare_targets(
        self,
        *,
        target_ids: list[str],
    ) -> dict[str, Any]:
        """Side-by-side substrate rollup for two or more target ids.

        For each ``target_id`` the rollup counts the substrate's live
        material: current facts (``superseded_by IS NULL AND valid_until
        IS NULL``), open nexuses (``valid_until IS NULL AND superseded_by
        IS NULL``), the hypothesis status mix, and a handful of recent
        findings (``analyst_outputs`` rows of ``kind = 'finding'`` that
        have not been superseded).  This is the comparator the agentic
        assessors lean on when the loop hands it several target ids — a
        single call returns one comparable shape per target rather than
        forcing N separate queries.

        Requires at least two target ids (a comparison of one is a
        degenerate rollup); fewer returns a structured error so the
        planner can correct.  Target ids past
        :data:`_COMPARE_MAX_TARGETS` are dropped so a runaway planner
        can't fan the rollup across the whole catalog.
        """
        # De-dupe while preserving order, drop blanks, and clamp the fan.
        seen: set[str] = set()
        ids: list[str] = []
        for raw in target_ids or []:
            tid = str(raw).strip()
            if tid and tid not in seen:
                seen.add(tid)
                ids.append(tid)
        ids = ids[:_COMPARE_MAX_TARGETS]
        if len(ids) < 2:
            return {
                "targets": [],
                "refs": [],
                "error": (
                    "compare_targets requires at least two distinct "
                    "target_ids"
                ),
            }

        targets: list[dict[str, Any]] = []
        refs: list[str] = []
        async with self._pool.acquire() as conn:
            for tid in ids:
                fact_count = await conn.fetchval(
                    """
                    SELECT count(*) FROM facts
                    WHERE target_id = $1
                      AND superseded_by IS NULL
                      AND valid_until IS NULL
                    """,
                    tid,
                )
                nexus_count = await conn.fetchval(
                    """
                    SELECT count(*) FROM nexuses
                    WHERE target_id = $1
                      AND valid_until IS NULL
                      AND superseded_by IS NULL
                    """,
                    tid,
                )
                hyp_rows = await conn.fetch(
                    """
                    SELECT status, count(*) AS n
                    FROM hypotheses
                    WHERE target_id = $1
                    GROUP BY status
                    """,
                    tid,
                )
                finding_rows = await conn.fetch(
                    """
                    SELECT id, title, confidence, severity, produced_at
                    FROM analyst_outputs
                    WHERE target_id = $1
                      AND kind = 'finding'
                      AND superseded_by IS NULL
                    ORDER BY produced_at DESC
                    LIMIT $2
                    """,
                    tid,
                    _COMPARE_RECENT_FINDINGS,
                )

                status_mix = {r["status"]: int(r["n"]) for r in hyp_rows}
                recent_findings: list[dict[str, Any]] = []
                for r in finding_rows:
                    fid = r["id"]
                    refs.append(str(fid))
                    recent_findings.append({
                        "id": str(fid),
                        "title": r["title"],
                        "confidence": float(r["confidence"])
                            if r["confidence"] is not None else None,
                        "severity": r["severity"],
                        "produced_at": r["produced_at"].isoformat()
                            if isinstance(r["produced_at"], datetime) else None,
                    })

                targets.append({
                    "target_id": tid,
                    "current_fact_count": int(fact_count or 0),
                    "open_nexus_count": int(nexus_count or 0),
                    "hypothesis_status_mix": status_mix,
                    "recent_findings": recent_findings,
                })

        return {
            "targets": targets,
            "refs": refs,
            "compared": [t["target_id"] for t in targets],
        }

    # ------------------------------------------------------------------
    # query_paths (P5 / #99) — signed paths A → … → B
    # ------------------------------------------------------------------

    async def query_paths(
        self,
        *,
        subject: str,
        obj: str,
        max_hops: int = 3,
        polarity_product: int | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Ranked signed PATHS from ``subject`` to ``obj`` over open nexuses.

        Walks the OPEN nexus graph (``valid_until IS NULL AND superseded_by
        IS NULL``) with a recursive CTE, treating each open nexus as a
        directed ``subject → object`` edge (the ``intermediary`` cut-out, if
        present, is a property of the edge, not a separate node) carrying a
        POLARITY sign (+1 / -1 / 0).  Returns paths of 1..``max_hops`` hops,
        each with the running **polarity product** — the structural-balance
        sign of the whole chain (an even number of -1 edges → +1 net
        "the enemy of my enemy"; odd → -1).

        The nexus graph is CYCLIC, so each branch carries the path-so-far as
        a ``text[]`` of ``lower(node)`` names; a candidate next hop whose
        object is already in the path is pruned (the VISITED-SET guard) so
        traversal terminates with no reliance on ``max_hops``.  ``max_hops``
        is clamped to :data:`_GRAPH_MAX_HOPS`; the frontier and the returned
        set are clamped to :data:`_GRAPH_MAX_PATHS`.

        When ``polarity_product`` (∈ {-1, 0, 1}) is supplied, only paths
        whose net sign matches are returned — e.g. ``-1`` surfaces net-
        antagonistic chains, ``+1`` net-supportive ones.  Paths are ranked
        shortest-first (fewest hops), then by descending min-confidence
        (the weakest link), so the tightest, best-evidenced chains lead.
        """
        s = (subject or "").strip()
        o = (obj or "").strip()
        if not s or not o:
            return {
                "subject": subject,
                "object": obj,
                "paths": [],
                "refs": [],
                "error": "both subject and object must be non-empty",
            }
        hops = max(1, min(int(max_hops), _GRAPH_MAX_HOPS))
        clamped_limit = max(1, min(int(limit), _GRAPH_MAX_PATHS))
        pol_filter = (
            int(polarity_product)
            if polarity_product is not None and int(polarity_product) in (-1, 0, 1)
            else None
        )

        # Recursive CTE. The base step seeds every open edge leaving
        # ``subject``; the recursive step extends a frontier path by one open
        # edge whose subject = the path's current head, pruning any hop whose
        # object is already on the path (VISITED-SET guard) and any path that
        # has reached ``obj`` (a terminal path is not extended further).
        sql = """
        WITH RECURSIVE walk AS (
            SELECT
                n.id                              AS edge_id,
                lower(n.subject)                  AS head,
                ARRAY[lower(n.subject), lower(n.object)] AS visited,
                ARRAY[n.id]                       AS edge_ids,
                ARRAY[n.subject, n.object]        AS node_path,
                ARRAY[n.polarity]::smallint[]     AS polarities,
                n.polarity::int                   AS pol_product,
                n.confidence                      AS min_conf,
                1                                 AS hops
            FROM nexuses n
            WHERE n.valid_until IS NULL
              AND n.superseded_by IS NULL
              AND lower(n.subject) = lower($1)
            UNION ALL
            SELECT
                n.id,
                lower(n.object),
                w.visited || lower(n.object),
                w.edge_ids || n.id,
                w.node_path || n.object,
                w.polarities || n.polarity,
                w.pol_product * n.polarity,
                least(w.min_conf, n.confidence),
                w.hops + 1
            FROM walk w
            JOIN nexuses n
              ON lower(n.subject) = w.head
            WHERE n.valid_until IS NULL
              AND n.superseded_by IS NULL
              AND w.hops < $3
              AND lower(w.node_path[array_upper(w.node_path, 1)]) <> lower($2)
              AND NOT (lower(n.object) = ANY(w.visited))
        )
        SELECT edge_ids, node_path, polarities, pol_product, min_conf, hops
        FROM walk
        WHERE lower(node_path[array_upper(node_path, 1)]) = lower($2)
        ORDER BY hops ASC, min_conf DESC
        LIMIT $4
        """
        # The recursive frontier itself is bounded by the visited-set guard +
        # the hop cap; the LIMIT caps the materialized terminal set.
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                sql, s, o, hops, clamped_limit + _GRAPH_MAX_PATHS
            )

        paths: list[dict[str, Any]] = []
        refs: list[str] = []
        for r in records:
            net = int(r["pol_product"]) if r["pol_product"] is not None else 0
            if pol_filter is not None and net != pol_filter:
                continue
            edge_ids = [str(e) for e in (r["edge_ids"] or [])]
            for e in edge_ids:
                if e not in refs:
                    refs.append(e)
            paths.append({
                "nodes": list(r["node_path"] or []),
                "edge_ids": edge_ids,
                "polarities": [int(p) for p in (r["polarities"] or [])],
                "polarity_product": net,
                "min_confidence": float(r["min_conf"])
                    if r["min_conf"] is not None else None,
                "hops": int(r["hops"]),
            })
            if len(paths) >= clamped_limit:
                break

        return {
            "subject": subject,
            "object": obj,
            "max_hops": hops,
            "polarity_product_filter": pol_filter,
            "paths": paths,
            "refs": refs,
        }

    # ------------------------------------------------------------------
    # find_proxy_chains (P5 / #99) — INDIRECT links A → … → B
    # ------------------------------------------------------------------

    async def find_proxy_chains(
        self,
        *,
        subject: str,
        obj: str,
        max_hops: int = 3,
        polarity_product: int | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """Proxy / cut-out chains from ``subject`` to ``obj`` — INDIRECT only.

        A specialization of :meth:`query_paths` that drops the trivial
        direct ``subject → object`` edge and surfaces only the INDIRECT
        links — multi-hop chains (hops >= 2) AND single edges that carry a
        non-null ``intermediary`` cut-out (the reified ``A → via → B``
        proxy edge).  This is the "proxy path from A to B" the tradecraft
        reads: how is A connected to B when they are not (only) directly
        connected.  Same cyclic-graph VISITED-SET guard, hop cap, and
        ``polarity_product`` filter as :meth:`query_paths`.
        """
        # Reuse the bounded walk, then keep only the indirect chains.
        base = await self.query_paths(
            subject=subject,
            obj=obj,
            max_hops=max_hops,
            polarity_product=polarity_product,
            limit=_GRAPH_MAX_PATHS,
        )
        if base.get("error"):
            return {
                "subject": subject,
                "object": obj,
                "chains": [],
                "refs": [],
                "error": base["error"],
            }
        clamped_limit = max(1, min(int(limit), _GRAPH_MAX_PATHS))

        # A single-edge path is "proxy" only if that edge reifies an
        # intermediary cut-out; pull the intermediary for the 1-hop edges so
        # we can keep the A→via→B reified proxies and drop bare A→B edges.
        single_edge_ids: list[str] = [
            p["edge_ids"][0]
            for p in base["paths"]
            if p["hops"] == 1 and p["edge_ids"]
        ]
        intermediary_by_edge: dict[str, str | None] = {}
        if single_edge_ids:
            async with self._pool.acquire() as conn:
                irows = await conn.fetch(
                    "SELECT id, intermediary FROM nexuses "
                    "WHERE id = ANY($1::uuid[])",
                    single_edge_ids,
                )
            intermediary_by_edge = {
                str(r["id"]): r["intermediary"] for r in irows
            }

        chains: list[dict[str, Any]] = []
        refs: list[str] = []
        for p in base["paths"]:
            if p["hops"] >= 2:
                indirect = True
                intermediary = None
            else:
                intermediary = intermediary_by_edge.get(p["edge_ids"][0]) \
                    if p["edge_ids"] else None
                indirect = bool(intermediary)
            if not indirect:
                continue
            chain = dict(p)
            if intermediary is not None:
                chain["intermediary"] = intermediary
            for e in p["edge_ids"]:
                if e not in refs:
                    refs.append(e)
            chains.append(chain)
            if len(chains) >= clamped_limit:
                break

        return {
            "subject": subject,
            "object": obj,
            "max_hops": base["max_hops"],
            "polarity_product_filter": base["polarity_product_filter"],
            "chains": chains,
            "refs": refs,
        }

    # ------------------------------------------------------------------
    # query_brokers (P5 / #99) — entities ON the paths between two camps
    # ------------------------------------------------------------------

    async def query_brokers(
        self,
        *,
        camp_a: list[str],
        camp_b: list[str],
        max_hops: int = 3,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Entities that SIT ON paths between two entity sets (brokers).

        A broker is an intermediate node that lies on a path from some
        member of ``camp_a`` to some member of ``camp_b`` (and is itself in
        neither camp).  Walks the open nexus graph from every ``camp_a``
        seed (same recursive CTE + VISITED-SET guard + hop cap as
        :meth:`query_paths`); for every walk that terminates on a
        ``camp_b`` member, the INTERIOR nodes of the path are credited as
        brokers.  Brokers are ranked by how many distinct A→B paths run
        through them (betweenness-flavored degree), then named.

        Both camps are clamped to :data:`_BROKER_MAX_CAMP` members and the
        result to :data:`_BROKER_MAX_RESULTS` brokers.
        """
        a_names = [str(x).strip() for x in (camp_a or []) if str(x).strip()]
        b_names = [str(x).strip() for x in (camp_b or []) if str(x).strip()]
        a_names = a_names[:_BROKER_MAX_CAMP]
        b_names = b_names[:_BROKER_MAX_CAMP]
        if not a_names or not b_names:
            return {
                "camp_a": camp_a,
                "camp_b": camp_b,
                "brokers": [],
                "refs": [],
                "error": "both camp_a and camp_b must be non-empty",
            }
        hops = max(1, min(int(max_hops), _GRAPH_MAX_HOPS))
        clamped_limit = max(1, min(int(limit), _BROKER_MAX_RESULTS))
        a_lower = [n.lower() for n in a_names]
        b_lower = [n.lower() for n in b_names]

        # Walk from every camp_a seed; keep terminal paths that land on a
        # camp_b member. The interior nodes (everything between the first and
        # last) are the brokers. VISITED-SET guard + hop cap as above.
        sql = """
        WITH RECURSIVE walk AS (
            SELECT
                lower(n.object)                          AS head,
                ARRAY[lower(n.subject), lower(n.object)]  AS visited,
                ARRAY[n.id]                              AS edge_ids,
                ARRAY[n.subject, n.object]               AS node_path,
                1                                        AS hops
            FROM nexuses n
            WHERE n.valid_until IS NULL
              AND n.superseded_by IS NULL
              AND lower(n.subject) = ANY($1::text[])
            UNION ALL
            SELECT
                lower(n.object),
                w.visited || lower(n.object),
                w.edge_ids || n.id,
                w.node_path || n.object,
                w.hops + 1
            FROM walk w
            JOIN nexuses n
              ON lower(n.subject) = w.head
            WHERE n.valid_until IS NULL
              AND n.superseded_by IS NULL
              AND w.hops < $3
              AND NOT (w.head = ANY($2::text[]))
              AND NOT (lower(n.object) = ANY(w.visited))
        )
        SELECT edge_ids, node_path
        FROM walk
        WHERE head = ANY($2::text[])
        LIMIT $4
        """
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                sql, a_lower, b_lower, hops, _GRAPH_MAX_PATHS
            )

        # Tally interior nodes (exclude the camp endpoints themselves).
        broker_paths: dict[str, int] = {}
        broker_display: dict[str, str] = {}
        broker_refs: dict[str, set[str]] = {}
        camp_set = set(a_lower) | set(b_lower)
        for r in records:
            node_path = list(r["node_path"] or [])
            edge_ids = [str(e) for e in (r["edge_ids"] or [])]
            interior = node_path[1:-1]  # drop the A endpoint and B endpoint
            for node in interior:
                key = node.lower()
                if key in camp_set:
                    continue
                broker_paths[key] = broker_paths.get(key, 0) + 1
                broker_display.setdefault(key, node)
                broker_refs.setdefault(key, set()).update(edge_ids)

        ranked = sorted(
            broker_paths.items(), key=lambda kv: kv[1], reverse=True
        )[:clamped_limit]
        refs: list[str] = []
        brokers: list[dict[str, Any]] = []
        for key, count in ranked:
            edge_ids = sorted(broker_refs.get(key, set()))
            for e in edge_ids:
                if e not in refs:
                    refs.append(e)
            brokers.append({
                "entity": broker_display.get(key, key),
                "path_count": count,
                "edge_ids": edge_ids,
            })

        return {
            "camp_a": a_names,
            "camp_b": b_names,
            "max_hops": hops,
            "brokers": brokers,
            "refs": refs,
        }

    # ==================================================================
    # JOURNAL SELF-INSTRUMENT readers (Journal Assessor Wave 1, plan §5).
    #
    # The journal is the ONE analyst pointed at the whole organism INCLUDING
    # ITSELF. These reads expose its own instruments — recent assessments, the
    # graph's shape (graph_mining / structural_balance), critic scores,
    # calibration (incl. the SEGREGATED brier_forecast_acute), what fired vs went
    # quiet, source health, governor/budget pressure, and what changed since the
    # last entry — so a self-narrative can be grounded in REAL metrics, not
    # mythologised. Each returns ``{... , "refs": [...]}`` (refs = substrate UUIDs
    # the journal may cite; the metric tables that carry no row UUID — graph_metrics
    # / calibration aggregates / budget rollups — return ``refs: []`` and are cited
    # by the journal as observations rather than chip-linked rows). All temporal
    # reads gate to "currently true" rows (the journal physically cannot re-assert
    # retired state). HONESTY (plan §10): calibration reports the unproven posture
    # straight from the substrate; the journal's deterministic honesty post-step
    # reads these same numbers.
    # ==================================================================

    async def get_assessments(
        self,
        *,
        analyst_id: str | None = None,
        target_id: str | None = None,
        since_hours: int | None = 48,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Recent per-target / global ASSESSMENTS — the platform's own findings
        from the LIVE producers: the bounded P2 units + the per-country and world
        COMPOSITIONS (plan §5).

        Distinct from ``list_findings`` only by intent: the journal narrates OVER
        the assessment conclusions, so this read defaults to the live producer set
        (``_ASSESSMENT_PRODUCER_ANALYSTS``) and folds the critic's
        ``overall_score`` in the same way
        (``effective_confidence = min(confidence, critic_score)``). With no
        ``analyst_id`` it returns rows from every live assessment producer — the
        four bounded units plus ``country_composition`` + ``world_assessor`` — so
        the journal sees the whole live assessment surface in one call (NOT the
        retired ``country_assessor`` monolith it defaulted to before). Open rows
        only (``superseded_by IS NULL``).
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        clauses: list[str] = ["f.kind = 'finding'", "f.superseded_by IS NULL"]
        params: list[Any] = []
        if analyst_id is not None:
            params.append(analyst_id)
            clauses.append(f"f.analyst_id = ${len(params)}")
        else:
            # Default to the LIVE assessment producers (the journal's reflection
            # surface) rather than every finding-producer OR the retired
            # country_assessor/world_assessor monolith. Parameterized (trusted
            # module constant, but keeps the ANY() out of the SQL literal).
            params.append(list(_ASSESSMENT_PRODUCER_ANALYSTS))
            clauses.append(f"f.analyst_id = ANY(${len(params)}::text[])")
        if target_id is not None:
            params.append(target_id)
            clauses.append(f"f.target_id = ${len(params)}")
        if since_hours is not None:
            params.append(
                datetime.now(timezone.utc) - timedelta(hours=int(since_hours))
            )
            clauses.append(f"f.produced_at >= ${len(params)}")
        params.append(clamped_limit)
        sql = (
            "SELECT f.id, f.title, f.body, f.confidence, f.severity, "
            "       f.target_id, f.analyst_id, f.produced_at, "
            "       c.critic_score AS critic_score "
            "FROM analyst_outputs f "
            "LEFT JOIN LATERAL ( "
            "  SELECT (cr.data->>'overall_score')::real AS critic_score "
            "  FROM analyst_outputs cr "
            "  WHERE cr.kind = 'critique' "
            "    AND cr.data->>'analyzed_output_id' = f.id::text "
            "    AND cr.data->>'overall_score' IS NOT NULL "
            "  ORDER BY cr.produced_at DESC, cr.id DESC LIMIT 1 "
            ") c ON TRUE "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY f.produced_at DESC, f.id DESC "
            f"LIMIT ${len(params)}"
        )
        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql, *params)
        rows: list[dict[str, Any]] = []
        refs: list[str] = []
        for r in records:
            refs.append(str(r["id"]))
            confidence = float(r["confidence"]) if r["confidence"] is not None else None
            cs = r["critic_score"]
            critic_score = float(cs) if cs is not None else None
            effective = (
                min(confidence, critic_score)
                if (confidence is not None and critic_score is not None)
                else confidence
            )
            rows.append({
                "id": str(r["id"]),
                "title": r["title"],
                "body": (r["body"] or "")[:2000],
                "confidence": confidence,
                "critic_score": critic_score,
                "effective_confidence": effective,
                "severity": r["severity"],
                "target_id": r["target_id"],
                "analyst_id": r["analyst_id"],
                "produced_at": r["produced_at"].isoformat()
                    if isinstance(r["produced_at"], datetime) else None,
            })
        return {"rows": rows, "refs": refs, "count": len(rows)}

    async def get_graph_structure(
        self,
        *,
        limit: int = 20,
    ) -> dict[str, Any]:
        """The knowledge graph's SHAPE — the latest ``graph_mining`` metrics
        (communities + modularity + top centrality) (plan §5).

        Reads the freshest ``graph_mining`` row from ``graph_metrics`` (the
        deterministic miner persists one per run). The journal narrates the
        graph's structure — how clustered the world is, who is central, where the
        brokers sit. Aggregate metric (no per-row UUID), so ``refs`` is empty: the
        journal cites it as an observation about its own graph instrument, not a
        chip-linked substrate row.
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        sql = (
            "SELECT payload, computed_at FROM graph_metrics "
            "WHERE metric_kind = 'graph_mining' "
            "ORDER BY computed_at DESC LIMIT 1"
        )
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(sql)
        if row is None:
            return {
                "available": False,
                "reason": "no graph_mining metric computed yet",
                "refs": [],
            }
        payload = row["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else (payload or {})
        top_centrality = payload.get("top_centrality") or {}
        # Clamp the centrality slice the journal sees (already capped to ~25
        # nodes upstream, but bound it here too).
        if isinstance(top_centrality, dict):
            top_centrality = dict(list(top_centrality.items())[:clamped_limit])
        interesting = payload.get("interesting") or []
        if isinstance(interesting, list):
            interesting = interesting[:clamped_limit]
        return {
            "available": True,
            "computed_at": row["computed_at"].isoformat()
                if isinstance(row["computed_at"], datetime) else None,
            "community_count": payload.get("community_count"),
            "modularity": payload.get("modularity"),
            "node_count": payload.get("node_count"),
            "edge_count": payload.get("edge_count"),
            "proxy_chain_count": payload.get("proxy_chain_count"),
            "top_centrality": top_centrality,
            "interesting": interesting,
            "refs": [],
        }

    async def get_structural_balance(
        self,
        *,
        limit: int = 20,
    ) -> dict[str, Any]:
        """The graph's TENSION — the latest ``structural_balance`` unstable
        (Heider-imbalanced ++− / --- ) triads + frustration map (plan §5).

        Reads the freshest ``structural_balance`` row from ``graph_metrics``. An
        unstable signed triad (sign-product negative) predicts realignment
        pressure — a PREDICTION of tension, not a settled fact (the journal must
        narrate it as such, per the self-anatomy MAP). Aggregate metric, so
        ``refs`` is empty.
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        sql = (
            "SELECT payload, computed_at FROM graph_metrics "
            "WHERE metric_kind = 'structural_balance' "
            "ORDER BY computed_at DESC LIMIT 1"
        )
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(sql)
        if row is None:
            return {
                "available": False,
                "reason": "no structural_balance metric computed yet",
                "refs": [],
            }
        payload = row["payload"]
        payload = json.loads(payload) if isinstance(payload, str) else (payload or {})
        unstable = payload.get("unbalanced_triads") or []
        if isinstance(unstable, list):
            unstable = unstable[:clamped_limit]
        frustration = payload.get("frustration") or {}
        if isinstance(frustration, dict):
            frustration = dict(
                sorted(frustration.items(), key=lambda kv: kv[1], reverse=True)[
                    :clamped_limit
                ]
            )
        interesting = payload.get("interesting") or []
        if isinstance(interesting, list):
            interesting = interesting[:clamped_limit]
        return {
            "available": True,
            "computed_at": row["computed_at"].isoformat()
                if isinstance(row["computed_at"], datetime) else None,
            "balance_ratio": payload.get("balance_ratio"),
            "balanced_count": payload.get("balanced_count"),
            "unbalanced_count": payload.get("unbalanced_count"),
            "unstable_triads": unstable,
            "frustration": frustration,
            "interesting": interesting,
            "note": (
                "an unstable (sign-product negative) triad predicts realignment "
                "pressure — a prediction of tension, NOT a settled fact"
            ),
            "refs": [],
        }

    async def get_critic_scores(
        self,
        *,
        analyst_id: str | None = None,
        since_hours: int | None = 168,
        limit: int = 20,
    ) -> dict[str, Any]:
        """The platform's OWN critic scores over recent outputs (plan §5).

        Reads recent ``kind='critique'`` rows from ``analyst_outputs`` (the live
        critique stream). HONESTY (self-anatomy MAP): the critic's ``overall_score``
        is structurally IGNORED on the live path today — reading the score is
        honest reflection, NOT a closed loop. ``analyst_id`` filters to critiques
        OF a given analyst's outputs (``data->>'analyzed_analyst_id'``).
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        clauses: list[str] = ["kind = 'critique'"]
        params: list[Any] = []
        if analyst_id is not None:
            params.append(analyst_id)
            clauses.append(f"data->>'analyzed_analyst_id' = ${len(params)}")
        if since_hours is not None:
            params.append(
                datetime.now(timezone.utc) - timedelta(hours=int(since_hours))
            )
            clauses.append(f"produced_at >= ${len(params)}")
        params.append(clamped_limit)
        sql = (
            "SELECT id, analyst_id, produced_at, data "
            "FROM analyst_outputs "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY produced_at DESC, id DESC "
            f"LIMIT ${len(params)}"
        )
        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql, *params)
        rows: list[dict[str, Any]] = []
        refs: list[str] = []
        scores_seen: list[float] = []
        for r in records:
            refs.append(str(r["id"]))
            raw = r["data"]
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
            overall = data.get("overall_score")
            if isinstance(overall, (int, float)):
                scores_seen.append(float(overall))
            rows.append({
                "id": str(r["id"]),
                "judge_analyst_id": r["analyst_id"],
                "analyzed_analyst_id": data.get("analyzed_analyst_id"),
                "analyzed_output_id": data.get("analyzed_output_id"),
                "overall_score": overall,
                "scores": data.get("scores") or {},
                "revision_delta": (
                    (str(data.get("revision_delta"))[:1000])
                    if data.get("revision_delta") else None
                ),
                "produced_at": r["produced_at"].isoformat()
                    if isinstance(r["produced_at"], datetime) else None,
            })
        mean_score = (
            round(sum(scores_seen) / len(scores_seen), 4) if scores_seen else None
        )
        return {
            "rows": rows,
            "refs": refs,
            "count": len(rows),
            "mean_overall_score": mean_score,
            "actuation_note": (
                "the critic's overall_score is structurally IGNORED on the live "
                "path today (NON-ACTUATING) — reading it is reflection, not a "
                "closed loop"
            ),
        }

    async def get_calibration(self) -> dict[str, Any]:
        """The platform's CALIBRATION posture — the latest ``kind='calibration'``
        finding, with the SEGREGATED acute-forecast pilot reported HONESTLY
        (plan §5 / §10).

        Reads the freshest calibration finding from ``analyst_outputs``. The
        headline ``brier`` is EXOGENOUS-only (the only number that measures
        calibration against reality); the acute-forecast pilot lives in its OWN
        keys (``brier_forecast_acute`` / ``brier_skill_score`` / sample size /
        ready / degenerate / status) and is NEVER pooled into the headline. This
        is the read the journal's deterministic honesty post-step (§10) keys off:
        the forecast leg is UNPROVEN until ``forecast_acute_ready`` AND NOT
        ``forecast_acute_degenerate`` AND ``brier_skill_score > 0``.
        """
        sql = (
            "SELECT id, produced_at, data FROM analyst_outputs "
            "WHERE kind = 'calibration' "
            "ORDER BY produced_at DESC, id DESC LIMIT 1"
        )
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(sql)
        if row is None:
            return {
                "available": False,
                "reason": "no calibration finding computed yet",
                "forecast_unproven": True,
                "calibration_thin": True,
                "refs": [],
            }
        raw = row["data"]
        data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        bss = data.get("brier_skill_score")
        ready = bool(data.get("forecast_acute_ready"))
        degenerate = bool(data.get("forecast_acute_degenerate"))
        # The honesty verdict, computed deterministically from the substrate (NOT
        # self-reported): the forecast leg counts as PROVEN only if it is ready,
        # non-degenerate, and has earned positive skill.
        forecast_proven = (
            ready and not degenerate and isinstance(bss, (int, float)) and bss > 0.0
        )
        exo_n = data.get("exogenous_sample_size")
        calibration_thin = not isinstance(exo_n, int) or exo_n < 5
        return {
            "available": True,
            "id": str(row["id"]),
            "produced_at": row["produced_at"].isoformat()
                if isinstance(row["produced_at"], datetime) else None,
            # Headline calibration (exogenous-only).
            "brier": data.get("brier"),
            "brier_exogenous": data.get("brier_exogenous"),
            "exogenous_sample_size": exo_n,
            "sample_size": data.get("sample_size"),
            "insufficient_exogenous": data.get("insufficient_exogenous"),
            "self_consistency_only": data.get("self_consistency_only"),
            # Segregated acute-forecast pilot (n<30, reported honestly).
            "brier_forecast_acute": data.get("brier_forecast_acute"),
            "brier_forecast_acute_raw": data.get("brier_forecast_acute_raw"),
            "brier_climatology": data.get("brier_climatology"),
            "brier_skill_score": bss,
            "forecast_acute_sample_size": data.get("forecast_acute_sample_size"),
            "forecast_acute_ready": ready,
            "forecast_acute_degenerate": degenerate,
            "forecast_acute_status": data.get("forecast_acute_status"),
            # The deterministic honesty verdict — the journal's §10 post-step
            # reads these directly so it can flag the unproven legs even if the
            # narrative omits them.
            "forecast_unproven": not forecast_proven,
            "calibration_thin": calibration_thin,
            "refs": [str(row["id"])],
        }

    async def get_run_health(
        self,
        *,
        analyst_id: str | None = None,
        quiet_hours: int = 24,
        limit: int = 40,
    ) -> dict[str, Any]:
        """What FIRED vs went QUIET — the dead-analyst self-diagnosis (plan §5).

        Rolls up ``analyst_traces`` to the LAST run per analyst (most-recent
        ``run_started_at``), with its status, whether it carried an error_payload,
        and how many hours ago it ran. Analysts whose last run is older than
        ``quiet_hours`` are flagged ``quiet=True`` — the journal's recreation of
        the pre-pivot bright spot (the agent diagnosing its own dormancy), now from
        real receipts. ``analyst_id`` narrows to one analyst's recent run history.
        ``refs`` is empty (a trace is keyed by run_id, not a citeable substrate
        row the chip walk resolves).
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        now = datetime.now(timezone.utc)
        if analyst_id is not None:
            # One analyst's recent run history.
            sql = (
                "SELECT run_id, analyst_id, status, cadence_trigger, "
                "       run_started_at, run_ended_at, "
                "       (error_payload IS NOT NULL) AS had_error "
                "FROM analyst_traces WHERE analyst_id = $1 "
                "ORDER BY run_started_at DESC LIMIT $2"
            )
            async with self._pool.acquire() as conn:
                records = await conn.fetch(sql, analyst_id, clamped_limit)
        else:
            # The LAST run per analyst (DISTINCT ON the freshest start).
            sql = (
                "SELECT DISTINCT ON (analyst_id) "
                "       run_id, analyst_id, status, cadence_trigger, "
                "       run_started_at, run_ended_at, "
                "       (error_payload IS NOT NULL) AS had_error "
                "FROM analyst_traces "
                "ORDER BY analyst_id, run_started_at DESC LIMIT $1"
            )
            async with self._pool.acquire() as conn:
                records = await conn.fetch(sql, clamped_limit)
        rows: list[dict[str, Any]] = []
        quiet: list[str] = []
        for r in records:
            started = r["run_started_at"]
            hours_ago: float | None = None
            if isinstance(started, datetime):
                hours_ago = round((now - started).total_seconds() / 3600.0, 1)
            is_quiet = hours_ago is None or hours_ago > quiet_hours
            if is_quiet and analyst_id is None and r["analyst_id"]:
                quiet.append(r["analyst_id"])
            rows.append({
                "analyst_id": r["analyst_id"],
                "run_id": str(r["run_id"]) if r["run_id"] is not None else None,
                "status": r["status"],
                "had_error": bool(r["had_error"]),
                "cadence_trigger": r["cadence_trigger"],
                "last_run_at": started.isoformat()
                    if isinstance(started, datetime) else None,
                "hours_ago": hours_ago,
                "quiet": is_quiet,
            })
        return {
            "rows": rows,
            "refs": [],
            "count": len(rows),
            "quiet_analysts": sorted(set(quiet)),
            "quiet_threshold_hours": quiet_hours,
        }

    async def get_source_health(
        self,
        *,
        silent_only: bool = False,
        silent_hours: int = 48,
        limit: int = 40,
    ) -> dict[str, Any]:
        """Source-poll HEALTH — which feeds are quiet or erroring (plan §5).

        Joins each head source descriptor to its most-recent ``signals`` time and
        its most-recent ``source_poll_outcomes`` row (migration 0046: only silent
        / failed polls are logged, so a NULL outcome with fresh signals = healthy).
        Lets the journal tell "no coverage on X" apart from "a quiet feed" and
        narrate the platform's intake honestly. ``refs`` is empty (source rows are
        descriptors, not chip-linked substrate rows).
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        sql = (
            "SELECT s.descriptor_id AS source_id, "
            "       s.body->'identity'->>'name' AS name, s.state, "
            "       sig.last_signal_at, po.outcome AS last_poll_outcome, "
            "       po.health_state AS last_health_state, "
            "       po.occurred_at AS last_poll_at "
            "FROM source_descriptors s "
            "LEFT JOIN LATERAL ( "
            "  SELECT max(fetched_at) AS last_signal_at FROM signals "
            "  WHERE source_id = s.descriptor_id "
            ") sig ON TRUE "
            "LEFT JOIN LATERAL ( "
            "  SELECT outcome, health_state, occurred_at FROM source_poll_outcomes "
            "  WHERE source_id = s.descriptor_id "
            "  ORDER BY occurred_at DESC LIMIT 1 "
            ") po ON TRUE "
            "WHERE s.is_head = TRUE AND s.state = 'active' "
            "ORDER BY sig.last_signal_at ASC NULLS FIRST "
            f"LIMIT {clamped_limit}"
        )
        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql)
        now = datetime.now(timezone.utc)
        rows: list[dict[str, Any]] = []
        silent_count = 0
        error_count = 0
        for r in records:
            lsa = r["last_signal_at"]
            silent_h: float | None = None
            if isinstance(lsa, datetime):
                silent_h = round((now - lsa).total_seconds() / 3600.0, 1)
            is_silent = silent_h is None or silent_h >= silent_hours
            if r["last_poll_outcome"] == "error":
                error_count += 1
            if is_silent:
                silent_count += 1
            if silent_only and not is_silent:
                continue
            rows.append({
                "source_id": r["source_id"],
                "name": r["name"],
                "state": r["state"],
                "last_signal_at": lsa.isoformat() if isinstance(lsa, datetime) else None,
                "silent_hours": silent_h,
                "last_poll_outcome": r["last_poll_outcome"],
                "last_health_state": r["last_health_state"],
            })
        return {
            "rows": rows,
            "refs": [],
            "count": len(rows),
            "silent_count": silent_count,
            "error_count": error_count,
            "silent_threshold_hours": silent_hours,
        }

    async def get_budget_status(
        self,
        *,
        analyst_id: str | None = None,
        demotion_lookback_hours: int = 168,
        limit: int = 40,
    ) -> dict[str, Any]:
        """Governor / BUDGET pressure — today's per-analyst token consumption +
        recent governor demotions/pauses (plan §5).

        Reads today's ``budget_ledger`` bucket (per-analyst tokens / runs / cost)
        and the recent ``budget_demotion_events`` (a demotion = a per-analyst or
        global cap hit forced a fallback). A governor PAUSE is a budget/rate cap,
        NOT an analytic finding (the journal must narrate it as plumbing, per the
        MAP). ``refs`` is empty (these are rollup rows, not citeable substrate).
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        ledger_clauses: list[str] = ["bucket = CURRENT_DATE"]
        ledger_params: list[Any] = []
        if analyst_id is not None:
            ledger_params.append(analyst_id)
            ledger_clauses.append(f"analyst_id = ${len(ledger_params)}")
        ledger_params.append(clamped_limit)
        ledger_sql = (
            "SELECT analyst_id, "
            "       sum(tokens_used) AS tokens_used, sum(runs) AS runs, "
            "       sum(cost_usd) AS cost_usd "
            f"FROM budget_ledger WHERE {' AND '.join(ledger_clauses)} "
            "GROUP BY analyst_id "
            "ORDER BY tokens_used DESC "
            f"LIMIT ${len(ledger_params)}"
        )
        demo_clauses: list[str] = []
        demo_params: list[Any] = []
        demo_params.append(
            datetime.now(timezone.utc) - timedelta(hours=int(demotion_lookback_hours))
        )
        demo_clauses.append(f"occurred_at >= ${len(demo_params)}")
        if analyst_id is not None:
            demo_params.append(analyst_id)
            demo_clauses.append(f"analyst_id = ${len(demo_params)}")
        demo_params.append(clamped_limit)
        demo_sql = (
            "SELECT analyst_id, cause, tokens_used_at_demote, tokens_cap_at_demote, "
            "       primary_llm, fallback_llm, occurred_at "
            f"FROM budget_demotion_events WHERE {' AND '.join(demo_clauses)} "
            "ORDER BY occurred_at DESC "
            f"LIMIT ${len(demo_params)}"
        )
        async with self._pool.acquire() as conn:
            ledger_records = await conn.fetch(ledger_sql, *ledger_params)
            demo_records = await conn.fetch(demo_sql, *demo_params)
        consumption: list[dict[str, Any]] = []
        for r in ledger_records:
            consumption.append({
                "analyst_id": r["analyst_id"],
                "tokens_used": int(r["tokens_used"] or 0),
                "runs": int(r["runs"] or 0),
                "cost_usd": float(r["cost_usd"] or 0.0),
            })
        demotions: list[dict[str, Any]] = []
        for r in demo_records:
            demotions.append({
                "analyst_id": r["analyst_id"],
                "cause": r["cause"],
                "tokens_used_at_demote": r["tokens_used_at_demote"],
                "tokens_cap_at_demote": r["tokens_cap_at_demote"],
                "primary_llm": r["primary_llm"],
                "fallback_llm": r["fallback_llm"],
                "occurred_at": r["occurred_at"].isoformat()
                    if isinstance(r["occurred_at"], datetime) else None,
            })
        return {
            "today_consumption": consumption,
            "recent_demotions": demotions,
            "demotion_count": len(demotions),
            "refs": [],
            "note": (
                "a governor demotion/pause is a budget/rate cap hit, NOT an "
                "analytic finding"
            ),
        }

    async def get_journal_delta(
        self,
        *,
        since: str | None = None,
        limit: int = 30,
    ) -> dict[str, Any]:
        """What CHANGED since the journal's last entry (plan §5 / §4.10).

        Returns the journal's own prior entry (the most recent ``entry``) + the
        current open consolidation (the "inner landscape") so the journal opens a
        run knowing where it left off (the surviving attentional-continuity thread,
        §7.5), AND a lightweight delta of substrate activity since ``since`` (an
        ISO8601 timestamp — defaults to the prior entry's ``period_end``): counts
        of new findings / situations / nexuses, so the journal sees at a glance
        what the platform metabolized this window. ``refs`` carries the prior
        entry + consolidation ids (the journal's own continuity, which it MAY cite
        — they are journal rows, off-chain, but they are real ids).
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        async with self._pool.acquire() as conn:
            prior_entry = await conn.fetchrow(
                "SELECT id, title, body, period_start, period_end, produced_at "
                "FROM journal_entries WHERE entry_kind = 'entry' "
                "ORDER BY period_end DESC, produced_at DESC LIMIT 1"
            )
            consolidation = await conn.fetchrow(
                "SELECT id, title, body, period_start, period_end, produced_at "
                "FROM journal_entries "
                "WHERE entry_kind = 'consolidation' "
                "  AND valid_until IS NULL AND superseded_by IS NULL "
                "ORDER BY produced_at DESC LIMIT 1"
            )
            # Resolve the delta window.
            since_dt: datetime | None = None
            if since:
                try:
                    since_dt = datetime.fromisoformat(str(since))
                    if since_dt.tzinfo is None:
                        since_dt = since_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    since_dt = None
            if since_dt is None and prior_entry is not None:
                since_dt = prior_entry["period_end"]
            if since_dt is None:
                since_dt = datetime.now(timezone.utc) - timedelta(hours=24)
            new_findings = await conn.fetchval(
                "SELECT count(*) FROM analyst_outputs "
                "WHERE kind = 'finding' AND superseded_by IS NULL "
                "  AND produced_at >= $1",
                since_dt,
            )
            new_situations = await conn.fetchval(
                "SELECT count(*) FROM situations "
                "WHERE superseded_by IS NULL AND updated_at >= $1",
                since_dt,
            )
            new_nexuses = await conn.fetchval(
                "SELECT count(*) FROM nexuses "
                "WHERE valid_until IS NULL AND superseded_by IS NULL "
                "  AND produced_at >= $1",
                since_dt,
            )

        def _entry_dict(r: Any) -> dict[str, Any] | None:
            if r is None:
                return None
            return {
                "id": str(r["id"]),
                "title": r["title"],
                "body": (r["body"] or "")[:4000],
                "period_start": r["period_start"].isoformat()
                    if isinstance(r["period_start"], datetime) else None,
                "period_end": r["period_end"].isoformat()
                    if isinstance(r["period_end"], datetime) else None,
                "produced_at": r["produced_at"].isoformat()
                    if isinstance(r["produced_at"], datetime) else None,
            }

        refs: list[str] = []
        if prior_entry is not None:
            refs.append(str(prior_entry["id"]))
        if consolidation is not None:
            refs.append(str(consolidation["id"]))
        _ = clamped_limit  # delta is a count rollup; limit reserved for future row lists
        return {
            "prior_entry": _entry_dict(prior_entry),
            "current_consolidation": _entry_dict(consolidation),
            "since": since_dt.isoformat() if isinstance(since_dt, datetime) else None,
            "delta": {
                "new_findings": int(new_findings or 0),
                "new_situations": int(new_situations or 0),
                "new_nexuses": int(new_nexuses or 0),
            },
            "refs": refs,
        }

    # ------------------------------------------------------------------
    # vector_search_by_embedding
    #
    # Not on the Protocol, but exposed as a helper for callers that
    # already have a vector in hand (e.g. the dedupe-tier-3 path or a
    # future embedder-aware wrapper).  Kept here so the qdrant query
    # logic lives in one place.
    # ------------------------------------------------------------------

    async def vector_search_by_embedding(
        self,
        *,
        query_embedding: list[float],
        target_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Qdrant similarity search against ``legba_signals`` by raw vector.

        Helper for callers that own an embedder.  Filters on the
        ``target_id`` payload field when supplied — matches the payload
        convention used by the dedupe-tier-3 upsert path
        (:mod:`legba.data.filters.dedupe`).
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        from qdrant_client.http import models as qmodels

        query_filter = None
        if target_id is not None:
            query_filter = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="target_id",
                        match=qmodels.MatchValue(value=target_id),
                    )
                ]
            )

        try:
            # qdrant-client >= 1.10 exposes ``query_points`` (returns a
            # ``QueryResponse`` with ``.points``); older clients exposed
            # ``search`` returning a list directly.  Support both so this
            # module isn't pinned to a single client version.
            if hasattr(self._qdrant, "query_points"):
                resp = await self._qdrant.query_points(
                    collection_name=self._signals_collection,
                    query=list(query_embedding),
                    limit=clamped_limit,
                    query_filter=query_filter,
                    with_payload=True,
                )
                hits = getattr(resp, "points", None) or []
            else:                                                # pragma: no cover
                hits = await self._qdrant.search(
                    collection_name=self._signals_collection,
                    query_vector=list(query_embedding),
                    limit=clamped_limit,
                    query_filter=query_filter,
                    with_payload=True,
                )
        except Exception as exc:                                # noqa: BLE001
            logger.warning(
                "substrate_query_port.vector_search.failed err=%s", exc,
            )
            return {
                "rows": [],
                "refs": [],
                "error": f"qdrant_search_failed: {exc!s}",
                "collection": self._signals_collection,
            }

        rows: list[dict[str, Any]] = []
        refs: list[str] = []
        for hit in hits or []:
            hid = getattr(hit, "id", None)
            if hid is None:
                continue
            hid_str = str(hid)
            refs.append(hid_str)
            payload = getattr(hit, "payload", None) or {}
            rows.append({
                "signal_id": hid_str,
                "target_id": payload.get("target_id"),
                "source_id": payload.get("source_id"),
                "external_id": payload.get("external_id"),
                "score": float(getattr(hit, "score", 0.0)),
            })

        return {
            "rows": rows,
            "refs": refs,
            "collection": self._signals_collection,
            "filtered_target_id": target_id,
        }
