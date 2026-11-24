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
``compare_targets``'s fact counts gate to current rows, and
``get_timeline`` anchors each item on a single timestamp (fact:
``valid_from`` → ``produced_at`` → ``created_at``; signal: ``fetched_at``
→ ``created_at``), skipping any row whose anchor resolves to NULL.

Implementation notes
--------------------

* ``search_signals`` runs a Postgres-native full-text search via
  ``to_tsvector('simple', payload->>'title' || ' ' || payload->>'summary')``
  against ``plainto_tsquery``.  The L-178 design brief mentions BM25 over
  a dedicated full-text engine as the preferred backing; the OpenSearch
  corpus readers (``search_corpus`` / ``read_document``) now cover that
  lexical-recall lane, so this stays the cheap title+summary FTS.  The
  old ``category`` filter argument was REMOVED (W2-T5 residual, 2026-07):
  0 of ~100k live signals carry a ``payload->>'category'`` key, so any
  value filtered every query to zero rows while looking like honest
  empties — worse than no filter.  Rows still surface the per-row
  ``category`` payload value when a signal carries one.

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
        opensearch_store=opensearch_store,  # Stage 1 — full-text corpus readers
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
from uuid import UUID

from . import _observations_read as _observations
from . import consult_health as _consult_health
from . import substrate_calendar_reads as _calendar
from . import substrate_cited_refs as _cited_refs
from . import substrate_corpus_readers as _corpus
from . import substrate_findings_reads as _findings
from . import substrate_frame_reads as _frames
from . import substrate_source_health as _source_health
from . import substrate_signal_search as _signal_search
from . import substrate_known_at as _known_at
from . import substrate_record_known_at as _record_cut
from . import substrate_entity_reads as _entity
from . import _observations_live_read as _live_obs
from . import substrate_due_resolvers as _due_resolvers
from . import substrate_graph_walks as _walks
from . import substrate_temporal as _temporal
from .target_resolution import stamp_target_resolution, widen_to_member_targets
from ..data import critic_fold as _critic_fold
from ..data import verified_findings as _verified
from ..data import rosters as _rosters
from ..data.calibration_honesty import reduce_calibration
from ..data.journal_evidence import MEMORY_ROW_FILTER_SQL
from ..data.provenance import origin as _origin

# Re-exported for existing consumers (tests/runtime/test_graph_walk_cutover.py
# imports both names from this module — the walk machinery moved to
# ``substrate_graph_walks`` at V3/P3, the import surface did not).
from .substrate_graph_walks import _ASSERTING_FAMILIES, _walk_families  # noqa: F401,E402

if TYPE_CHECKING:  # pragma: no cover
    import asyncpg

logger = logging.getLogger(__name__)



__all__ = ["PostgresQdrantSubstrateQueryPort"]


# Cap how many rows any one tool round can return.  The consult kind
# caps individual tool calls at ``limit`` (planner-supplied) but we
# clamp here too so a runaway planner can't ask for 10_000 rows and
# wedge a postgres connection.
_MAX_ROW_LIMIT = 200

# ``_FEED_EMPTY_SLACK`` and the source-health read live in
# ``substrate_source_health`` (wave Q split); the slack is re-exported here
# so its existing readers keep their import.
from .substrate_source_health import _FEED_EMPTY_SLACK  # noqa: E402,F401
from .substrate_cited_refs import CITED_REFS_CLASSIFY_CAP  # noqa: E402,F401
# ``CITED_REFS_CLASSIFY_CAP`` (the census read's bound) lives with the read in
# ``substrate_cited_refs``; re-exported so the port stays the one import.


def critic_folded_findings_sql(
    where: str, limit_param: int, *, critic_as_of: str = "",
    offset_param: int | None = None,
) -> str:
    """The critic-folded findings read, SET-BASED (2026-09-24, crossroads
    proof round).

    The former shape was a ``LEFT JOIN LATERAL (... ORDER BY cr.produced_at
    DESC LIMIT 1)`` per finding. The planner served that lateral from the
    ``(kind, produced_at DESC)`` index — walk critiques newest-first, stop at
    the first whose ``analyzed_output_id`` matches — which is quick for a
    finding that HAS a recent critique and a full walk of every critique row
    (~49k, each a jsonb deref) for a finding that has none. The critic
    samples, so most findings have none: the first forced crossroads run
    timed both ``list_findings`` and ``get_assessments`` out at 60 s, and a
    live ``EXPLAIN`` of the 24 h / 200-row read did not finish in 120 s.

    This shape picks the page of findings FIRST (a materialized CTE, at most
    ``_MAX_ROW_LIMIT`` rows), then fetches the latest scored critique per
    finding id through the expression index
    ``idx_analyst_outputs_critique_analyzed_output_id`` (``DISTINCT ON``),
    and joins the two. Live: 226 ms for the same read. The column list is
    byte-identical to the lateral form, so every caller's row loop is
    unchanged, and the SQL still takes its parameters positionally so
    :func:`widen_to_member_targets` can re-run it with one param swapped.

    ``critic_as_of`` is the optional ``AND cr.produced_at <= $n`` fragment
    that dates the fold to a decision instant (``believed_as_of``).
    ``offset_param`` (W11-F150) pages the read; without it the SQL is
    byte-identical to the unpaged form.

    H17: the fold CTE itself is now built by
    :func:`legba.data.critic_fold.latest_critique_cte`, the ONE definition the
    other twenty-one reads of "the latest critique for this finding" were moved
    onto. Two knobs keep this caller's measured plan exactly as it was pinned:
    ``title_like=None`` (this fold reads ANY scored critique, unlike the
    faithfulness-pinned reads) and ``ids_as_array=False`` (the ``IN`` form,
    whose plan is identical here because ``f`` carries a LIMIT and so estimates
    exactly — see the helper's module docstring).
    """
    fold = _critic_fold.latest_critique_cte(
        "c",
        "(cr.data->>'overall_score')::real AS critic_score",
        "SELECT id::text FROM f",
        title_like=None,
        extra_where=critic_as_of.strip().removeprefix("AND").strip(),
        ids_as_array=False,
    )
    return (
        "WITH f AS MATERIALIZED ( "
        "  SELECT f.id, f.title, f.body, f.confidence, f.severity, "
        "         f.target_id, f.analyst_id, f.produced_at "
        "  FROM analyst_outputs f "
        f" WHERE {where} "
        "  ORDER BY f.produced_at DESC, f.id DESC "
        f" LIMIT ${limit_param} "
        + (f"OFFSET ${offset_param} " if offset_param is not None else "")
        + f"), {fold} "
        "SELECT f.id, f.title, f.body, f.confidence, f.severity, "
        "       f.target_id, f.analyst_id, f.produced_at, "
        "       c.critic_score AS critic_score "
        "FROM f LEFT JOIN c ON c.fid = f.id::text "
        "ORDER BY f.produced_at DESC, f.id DESC"
    )

# H-2 (audit W6 / B0-5) — bounds on the journal's scorecard↔composition
# disagreements reconciliation surfaced on ``get_assessments``. A bounded,
# fail-safe REFLECTION surface (never a gate): how many distinct countries to
# reconcile per call and how many divergence rows to hand the journal.
_DISAGREEMENT_MAX_TARGETS = 12
_DISAGREEMENT_MAX_ROWS = 20

# ``search_context`` (S5-T4) — RAG chunks are short; a handful of the most
# similar priors answers "what does the corpus say about X". Default small,
# hard-capped so a runaway planner can't pull the whole corpus.
_SEARCH_CONTEXT_DEFAULT_K = 6
_SEARCH_CONTEXT_MAX_K = 50

# ``search_corpus`` / ``read_document`` (Stage 1 — the OpenSearch full-text
# corpus, index ``legba_signals_corpus``): BM25 lexical search over the WHOLE
# raw signal body + a by-id fetch of one document. Both now live in
# :mod:`legba.runtime.substrate_corpus_readers`, which owns the size cap and
# the filter whitelist; the default size is re-exported here because it is this
# module's public method signature.
_SEARCH_CORPUS_DEFAULT_SIZE = _corpus.SEARCH_CORPUS_DEFAULT_SIZE

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
    # D-6 (2026-09-04) — the three ids this list has been silently missing, and
    # the new one. `region_composition` and `escalation_composition` have been
    # live producers since S2-T2/S2-T4 while the comment above still said region
    # "joins this set when that leg lands"; an unlisted id makes `get_assessments`
    # answer a confident empty and name it as "not a live assessment producer",
    # which is a false sentence about a producer writing rows every cycle.
    "region_composition",
    "escalation_composition",
    # W5 (2026-11-07): the second thematic composition, listed for the same
    # reason escalation_composition is.
    "hybrid_threat_composition",
    # The ASSESSMENT CHANNEL (D-1 §2). After the demotion this is the surface
    # that ARGUES; the compositions above carry their inputs' words. The journal
    # and consult read this list, and a reflection surface that cannot see the
    # one interpretive read is reading the record and missing the reading.
    "world_assessment",
    # P3 LANE A — the per-COUNTRY interpretive read. Listed for exactly the
    # reason the comment above gives: an unlisted producer makes
    # ``get_assessments`` answer a confident empty and NAME it as "not a live
    # assessment producer", which is a false sentence the moment the channel is
    # transitioned active.
    "country_assessment",
    # The bounded units (first-order per-country reads the compositions fuse):
    # the P2 four + the S1-T4/T5/T7 broad units, fused by country_composition +
    # banded as fixed scorecard dimensions — the profile's ``rosters.scored_units``
    # (ROSTER-1), the same helper scorecard_banding.DIMENSIONS reads.
    *_rosters.scored_unit_roster(),
)

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
#
# V3/P3: the machinery (SQL templates, caps, endpoint resolution, the three
# walk functions) lives in :mod:`legba.runtime.substrate_graph_walks`; the
# port methods below delegate. The extraction is the module-size gate's own
# instruction — the temporal parameters could not fit in the ~150 lines of
# headroom this file had — and it is what P6's event tools will sit beside.
# ------------------------------------------------------------------


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
        world_context_collection: str = "world_context",
        tradecraft_collection: str = "tradecraft",
        opensearch_store: Any | None = None,
        corpus_index: str = "legba_signals_corpus",
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
        # S5-T4 ``search_context`` — the two Lane-4 RAG corpora (S5-T2). A
        # ``corpus`` filter narrows to one; with none the tool searches BOTH
        # and merges by score. Keyed by the corpus token the loader stamps on
        # every chunk payload (``payload['corpus']``) so a caller narrows with
        # the same name the descriptor advertises.
        self._context_collections: dict[str, str] = {
            "world_context": world_context_collection,
            "tradecraft": tradecraft_collection,
        }
        # Stage 1 ``search_corpus`` / ``read_document`` — the OpenSearch
        # full-text corpus (index ``corpus_index``, ~106k signal docs). Built
        # GUARDED + threaded in at bring-up (opensearch-py may be absent on a
        # host); when None the readers report the honest ``no_corpus_wired``
        # shape instead of connecting — the same degrade-not-fabricate contract
        # the embedder honors with ``no_embedder_wired``. The store's
        # ``connect()`` is idempotent + opens no socket until a real request, so
        # the readers connect lazily on first use and construction stays sync.
        self._opensearch = opensearch_store
        self._corpus_index = corpus_index

    @property
    def embedder(self) -> Any | None:
        """The port-threaded hosted embedder (``embed.primary.openai_compat``),
        or None. W4-F1: the consult watch mint's semantic fold reads it here —
        the consult deps carry no other handle on the embed plane."""
        return self._embedder

    # ------------------------------------------------------------------
    # search_signals
    # ------------------------------------------------------------------

    async def search_signals(
        self,
        *,
        query: str,
        limit: int = 20,
        scope_predicate: str | None = None,
        known_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Full-text search over ``signals`` — body in
        :mod:`legba.runtime.substrate_signal_search` (``known_at``: lane E0's
        record-time cut, evaluation harness only)."""
        return await _signal_search.search_signals(
            self._pool, query=query, limit=limit, scope_predicate=scope_predicate,
            max_limit=_MAX_ROW_LIMIT, known_at=known_at,
        )

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
        as_of: str | None = None,
        include_origin: list[str] | None = None,
        known_at: datetime | None = None,
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

        V3/P3 — ``as_of`` (ISO-8601, validity time) swaps that gate for the
        canonical as-of predicate (:mod:`substrate_temporal`): the facts
        that held on date D, INCLUDING rows superseded or expired since. A
        malformed value refuses rather than silently reading "now". The
        envelope then carries ``unbounded_start`` — how many returned rows
        have no recorded ``valid_from`` (over-included by construction).

        V3/P7 — the gate now carries the ORIGIN-CLASS leg (migration 0209):
        the open read is ``live_gate_sql`` (the 0032 pair +
        ``origin_class IN ('live','web_retrieval','seed')``), so a history
        class can never read as current. On the ``as_of`` path
        ``include_origin`` names the classes the read may see — ``None``
        means :data:`LIVE_CLASSES`, so a future backfill row is invisible to
        an as-of read unless the reader asks. An unknown class refuses loud
        rather than answering empty.

        E2 — ``known_at`` (evaluation harness only): ``created_at < K``, and
        the read becomes the as-of read at ``min(as_of, K)``
        (:mod:`legba.data.raw_reader_known_at`).
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
        try:
            as_of_dt = (
                _temporal.parse_instant(as_of, name="as_of")
                if as_of is not None else None
            )
        except _temporal.TemporalParameterError as exc:
            return {"rows": [], "refs": [], "error": str(exc)}
        if known_at is not None:
            as_of_dt = _record_cut.clamp(as_of_dt, known_at)
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))

        # Current-facts gate (Piece-B follow-up): never surface superseded
        # or expired rows.  See migration 0032 — NULL on both columns is the
        # canonical "open / live" fact; migration 0209 (V3/P7) adds the
        # origin-class leg to it.  With ``as_of`` the gate becomes the
        # as-of predicate: the open-row pair is deliberately NOT applied
        # (a row closed today was the answer on D), and ``superseded_by``
        # drops out because ``valid_until`` alone carries the close.
        try:
            origin_clause = _origin.origin_class_clause("", include_origin)
        except _origin.OriginClassError as exc:
            return {"rows": [], "refs": [], "error": str(exc)}
        clauses: list[str] = []
        params: list[Any] = []
        if as_of_dt is not None:
            params.append(as_of_dt)
            clauses.append(_temporal.temporal_predicate("", len(params)))
            # P7 — the as-of read still defaults to the live classes; a
            # history-class row answers only when include_origin asks for it.
            clauses.append(origin_clause)
        elif include_origin is not None:
            clauses.extend(["superseded_by IS NULL", "valid_until IS NULL"])
            clauses.append(origin_clause)
        else:
            clauses.append(_origin.live_gate_sql(""))
        if known_at is not None:
            params.append(known_at)
            clauses.append(_record_cut.created_before("created_at", len(params)))
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

        out: dict[str, Any] = {
            "rows": rows,
            "refs": refs,
            "filters": {
                "subject": subject,
                "predicate": predicate,
                "value": value,
            },
        }
        if as_of_dt is not None:
            # The as-of contract's honest counter: how much of this answer
            # rests on a start date nobody recorded (NULL valid_from is
            # over-included as '-infinity' by the predicate).
            out["as_of"] = as_of_dt.isoformat()
            out["unbounded_start"] = _temporal.unbounded_start(rows)
        if known_at is not None:
            return _record_cut.stamp(out, known_at, how="facts.created_at < K; validity at min(as_of, K)")
        return out

    # ------------------------------------------------------------------
    # inspect_entity
    # ------------------------------------------------------------------

    async def inspect_entity(
        self, *, name: str, known_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Return the latest profile + recent versions + recent mentions.

        Matches on ``LOWER(canonical_name) = LOWER($1)`` per the unique
        index in migration 0002.  Returns an empty-but-shaped result when
        no entity is found (rather than raising) so the planner can fall
        back to ``search_signals`` / ``query_facts`` without a crash.

        The body lives in :mod:`legba.runtime.substrate_entity_reads`
        (``known_at``: lane E2's record-time cut, evaluation harness only).
        """
        return await _entity.inspect_entity(self._pool, name=name, known_at=known_at)

    # ------------------------------------------------------------------
    # vector_search
    # ------------------------------------------------------------------

    async def vector_search(
        self,
        *,
        query: str,
        limit: int = 10,
        known_at: datetime | None = None,
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
            query_embedding=vec,
            limit=clamped_limit if known_at is None else _MAX_ROW_LIMIT,
        )
        # Carry the caller's original free-text query through + tag the
        # backing so the consult trace shows the semantic path ran.
        result["query"] = query
        result["backing"] = "qdrant_cosine"
        if known_at is not None:  # E0: Qdrant has no record time — over-fetch, confirm
            result = await _known_at.cut_result_rows(
                self._pool, result, known_at, id_key="signal_id", limit=clamped_limit,
                how="over-fetch, then signals.created_at < known_at")
        return result

    # ------------------------------------------------------------------
    # search_context (S5-T4) — RAG over the Lane-4 curated corpora
    # ------------------------------------------------------------------

    async def search_context(
        self,
        *,
        query: str,
        corpus: str | None = None,
        country: str | None = None,
        k: int = _SEARCH_CONTEXT_DEFAULT_K,
    ) -> dict[str, Any]:
        """Semantic search over the Lane-4 RAG corpora (S5-T4).

        Embeds the free-text ``query`` through the same port-threaded embedder
        as ``vector_search`` (S5-T1), then cosine-searches the S5-T2 corpus
        collections — ``world_context`` (country/topic priors, doctrine
        summaries) and ``tradecraft`` (analytic standards / SAT handbooks) —
        and returns the top-``k`` chunks with their loader-stamped metadata
        (``corpus`` / ``doc_id`` / ``title`` / ``section`` / ``countries`` /
        ``source_url`` / ``effective_date``; see
        :func:`legba.data.rag.lane4_loader._build_payload`).

        Optional filters:

          * ``corpus`` — narrow to one corpus (``world_context`` /
            ``tradecraft``); with none we search BOTH and merge by score. An
            unknown corpus token returns a structured error so the planner can
            correct rather than silently searching nothing.
          * ``country`` — a payload filter on the ``countries`` array (a chunk
            tagged for that country); Qdrant ``MatchAny`` over the field.
          * ``k`` — top-k, clamped to ``[1, _SEARCH_CONTEXT_MAX_K]``.

        HONESTY / degrade-not-drop (mirrors ``vector_search``'s seam-#11
        contract): no embedder wired → the honest ``no_embedder_wired``
        ``unavailable`` shape (never a fabricated vector); an empty query
        short-circuits (no embed round-trip); an embed failure degrades to
        ``unavailable``; a per-collection Qdrant error is logged and that
        collection is skipped (the other corpus still contributes) rather than
        failing the whole call.

        REF HONESTY (W2-T4 residual): chunk ids are ``ctx:``-prefixed —
        they are Qdrant uuid5 point ids over the BACKGROUND corpora, not
        substrate rows, and nothing downstream can dereference them as
        substrate.  The prefix means the consult loop's lineage coercion
        (``_coerce_uuid_list``) EXCLUDES them from
        ``cited_substrate_refs``/``derived_from`` BY DESIGN, so a chunk id
        can never masquerade as a citable substrate UUID.  The parallel
        ``context_refs`` list carries the same ``ctx:`` refs so the loop /
        trace can still state honestly what background material was read.
        """
        clamped_k = max(1, min(int(k), _SEARCH_CONTEXT_MAX_K))
        corpus_norm = (corpus or "").strip().lower() or None
        country_norm = (country or "").strip() or None

        # Resolve which corpus collections to search. An unknown corpus is a
        # structured error (the loader refuses arbitrary corpora too).
        if corpus_norm is not None and corpus_norm not in self._context_collections:
            return {
                "rows": [],
                "refs": [],
                "context_refs": [],
                "count": 0,
                "query": query,
                "corpus": corpus,
                "country": country,
                "k": clamped_k,
                "error": (
                    f"unknown corpus {corpus!r} — known corpora: "
                    f"{', '.join(sorted(self._context_collections))}"
                ),
            }
        corpora = (
            [corpus_norm] if corpus_norm else list(self._context_collections)
        )

        # No embedder threaded through the port — honest unavailable shape,
        # never a fabricated vector (the same contract vector_search honors).
        if self._embedder is None:
            return {
                "rows": [],
                "refs": [],
                "context_refs": [],
                "count": 0,
                "query": query,
                "corpus": corpus,
                "country": country,
                "k": clamped_k,
                "unavailable": True,
                "reason": (
                    "no_embedder_wired — search_context requires an embedding "
                    "model surfaced through this port; wire an embedding "
                    "service at bring-up (embed.primary.openai_compat)"
                ),
            }

        q = (query or "").strip()
        if not q:
            return {
                "rows": [],
                "refs": [],
                "context_refs": [],
                "count": 0,
                "query": query,
                "corpus": corpus,
                "country": country,
                "k": clamped_k,
                "backing": "qdrant_cosine",
                "note": "empty_query",
            }

        try:
            vec = await self._embedder.embed(q)
        except Exception as exc:  # noqa: BLE001 — embed backend surface
            logger.warning(
                "substrate_query_port.search_context.embed_failed err=%s", exc,
            )
            return {
                "rows": [],
                "refs": [],
                "context_refs": [],
                "count": 0,
                "query": query,
                "corpus": corpus,
                "country": country,
                "k": clamped_k,
                "unavailable": True,
                "reason": f"embed_failed: {exc!s}",
            }

        # Optional country filter over the ``countries`` payload array.
        query_filter = None
        if country_norm is not None:
            from qdrant_client.http import models as qmodels
            query_filter = qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="countries",
                        match=qmodels.MatchAny(any=[country_norm]),
                    )
                ]
            )

        merged: list[dict[str, Any]] = []
        searched: list[str] = []
        for corpus_name in corpora:
            collection = self._context_collections[corpus_name]
            try:
                hits = await self._search_context_collection(
                    collection, vec, limit=clamped_k, query_filter=query_filter,
                )
            except Exception as exc:  # noqa: BLE001 — degrade, don't fail the call
                logger.warning(
                    "substrate_query_port.search_context.search_failed "
                    "corpus=%s err=%s", corpus_name, exc,
                )
                continue
            searched.append(corpus_name)
            for hit in hits:
                row = self._map_context_hit(hit, corpus_name)
                if row is not None:
                    merged.append(row)

        # Merge across corpora by score (cosine — higher is closer), clamp to k.
        merged.sort(
            key=lambda r: r["score"] if r["score"] is not None else -1.0,
            reverse=True,
        )
        merged = merged[:clamped_k]
        # W2-T4 REF HONESTY: chunk_ids are already ``ctx:``-prefixed by
        # ``_map_context_hit``. ``refs`` carries them so the trace shows the
        # reads, but the loop's ``_coerce_uuid_list`` drops non-UUID strings —
        # so they are EXCLUDED from substrate lineage by design, never
        # masquerading as citable substrate UUIDs. ``context_refs`` is the
        # explicit parallel list for surfaces that want the background reads.
        context_refs = [r["chunk_id"] for r in merged]
        return {
            "rows": merged,
            "refs": list(context_refs),
            "context_refs": context_refs,
            "refs_note": (
                "ctx:-prefixed refs are background-corpus chunks, NOT "
                "substrate rows — non-citable; excluded from substrate "
                "lineage by design"
            ),
            "count": len(merged),
            "query": query,
            "corpus": corpus,
            "country": country,
            "k": clamped_k,
            "corpora_searched": searched,
            "backing": "qdrant_cosine",
        }

    async def _search_context_collection(
        self,
        collection: str,
        query_embedding: list[float],
        *,
        limit: int,
        query_filter: Any | None = None,
    ) -> list[Any]:
        """Cosine-search one RAG corpus collection by raw vector.

        Client-version tolerant (``query_points`` on qdrant-client >= 1.10 else
        the legacy ``search``), mirroring ``vector_search_by_embedding`` and
        ``grounding._search_world_context`` so this isn't pinned to one client.
        """
        vec = list(query_embedding)
        if hasattr(self._qdrant, "query_points"):
            resp = await self._qdrant.query_points(
                collection_name=collection,
                query=vec,
                limit=int(limit),
                query_filter=query_filter,
                with_payload=True,
            )
            return list(getattr(resp, "points", None) or [])
        hits = await self._qdrant.search(  # pragma: no cover — legacy client
            collection_name=collection,
            query_vector=vec,
            limit=int(limit),
            query_filter=query_filter,
            with_payload=True,
        )
        return list(hits or [])

    def _map_context_hit(
        self, hit: Any, corpus_name: str,
    ) -> dict[str, Any] | None:
        """Map one Qdrant hit onto a ``search_context`` row.

        Reads the Lane-4 payload shape (see
        :func:`legba.data.rag.lane4_loader._build_payload`). A hit with no
        readable ``text`` is dropped (an empty chunk answers nothing); the
        Qdrant point id (a deterministic ``uuid5``) becomes the auditable
        ``chunk_id`` — ``ctx:``-prefixed (W2-T4 ref honesty) so a chunk id
        is visibly NOT a substrate UUID and can never enter substrate
        lineage (``_coerce_uuid_list`` drops non-UUID strings by design).
        ``corpus`` prefers the payload's own value, falling back
        to the collection the hit came from.
        """
        hid = getattr(hit, "id", None)
        if hid is None:
            return None
        payload = getattr(hit, "payload", None) or {}
        if not isinstance(payload, dict):
            return None
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        score = getattr(hit, "score", None)
        countries = payload.get("countries")
        return {
            "chunk_id": f"ctx:{hid}",
            "corpus": payload.get("corpus") or corpus_name,
            "doc_id": payload.get("doc_id"),
            "title": payload.get("title"),
            "section": payload.get("section"),
            "countries": list(countries) if isinstance(countries, list) else [],
            "source_url": payload.get("source_url"),
            "effective_date": payload.get("effective_date"),
            "text": text,
            "score": float(score) if isinstance(score, (int, float)) else None,
        }

    # ------------------------------------------------------------------
    # search_corpus / read_document (Stage 1) — the OpenSearch full-text corpus
    # ------------------------------------------------------------------

    async def search_corpus(
        self,
        *,
        query: str,
        filters: dict[str, Any] | None = None,
        size: int = _SEARCH_CORPUS_DEFAULT_SIZE,
        known_at: datetime | None = None,
    ) -> dict[str, Any]:
        """BM25 lexical search over the OpenSearch signal corpus (Stage 1).

        Delegates to :func:`legba.runtime.substrate_corpus_readers.search_corpus`
        — see that module for the shape, the filter whitelist and the citable-ref
        invariant the 2026-09-16 review put on this reader.
        """
        return await _corpus.search_corpus(
            self._opensearch,
            self._corpus_index,
            query=query,
            filters=filters,
            size=size,
            pool=self._pool,
            known_at=known_at,
        )

    async def read_document(
        self, *, doc_id: str, known_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Fetch one document's body by id, across every id namespace (Stage 1).

        Delegates to
        :func:`legba.runtime.substrate_corpus_readers.read_document` — see that
        module for the corpus → signals → canonical-twin → analyst_outputs
        fallback ladder and the bounded, citable projection.
        """
        return await _corpus.read_document(
            self._opensearch, self._corpus_index, self._pool, doc_id=doc_id,
            known_at=known_at,
        )

    async def verified_receipt(self, *, doc_id: str) -> bool:
        """X3/G0 — is ``doc_id`` cited by an ADMITTED finding (arm C's scope)?

        A malformed id is never a receipt. See
        :func:`legba.data.verified_findings.verified_receipt_sql`.
        """
        try:
            sid = UUID(str(doc_id).strip())
        except (TypeError, ValueError):
            return False
        async with self._pool.acquire() as conn:
            return bool(await conn.fetchval(
                _verified.verified_receipt_sql(), sid, _verified.verify_floor(),
            ))

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
        as_of: str | None = None,
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

        V3/P3 — ``as_of`` (ISO-8601, validity time) reads the nexuses that
        held on date D instead: the canonical as-of predicate replaces the
        open-row gate, so a relationship superseded or expired since D still
        answers. A malformed value refuses; ``unbounded_start`` in the
        envelope counts returned rows with no recorded ``valid_from``.
        """
        try:
            as_of_dt = (
                _temporal.parse_instant(as_of, name="as_of")
                if as_of is not None else None
            )
        except _temporal.TemporalParameterError as exc:
            return {"rows": [], "refs": [], "error": str(exc)}
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))

        # Open-nexus gate — "what holds now" is the single open row. With
        # ``as_of`` the gate becomes the as-of predicate: the open-row pair
        # is deliberately dropped and ``valid_until`` alone carries the
        # close (spec §3.2).
        clauses: list[str] = []
        params: list[Any] = []
        if as_of_dt is not None:
            params.append(as_of_dt)
            clauses.append(_temporal.temporal_predicate("", len(params)))
        else:
            clauses.extend(["valid_until IS NULL", "superseded_by IS NULL"])
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

        out: dict[str, Any] = {
            "rows": rows,
            "refs": refs,
            "filters": {
                "subject": subject,
                "object": obj,
                "rel_type": rel_type,
                "polarity": polarity,
            },
        }
        if as_of_dt is not None:
            out["as_of"] = as_of_dt.isoformat()
            out["unbounded_start"] = _temporal.unbounded_start(rows)
        return out

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

        EMPTY-TARGET RESOLUTION (2026-09-16 review, defect 3). An explicit
        ``target_id`` that matches no row no longer returns a bare empty set:
        see :meth:`_resolve_empty_target_rows`. ``situation_iran_war`` has zero
        hypotheses of its own while its constituent desks have plenty, and
        "zero" was the most misleading answer this port could give.
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))

        clauses: list[str] = []
        params: list[Any] = []
        target_param: int | None = None
        if target_id is not None:
            params.append([str(target_id)])
            target_param = len(params)
            clauses.append(f"target_id = ANY(${len(params)}::text[])")
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

        resolution: dict[str, Any] | None = None
        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql, *params)
            if not records and target_param is not None:
                records, resolution = await widen_to_member_targets(
                    conn, sql, params, target_param, str(target_id),
                )

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

        out: dict[str, Any] = {
            "rows": rows,
            "refs": refs,
            "count": len(rows),
            "filters": {
                "target_id": target_id,
                "status": status,
                "situation_id": situation_id,
            },
        }
        stamp_target_resolution(out, str(target_id), resolution)
        return out

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
        include_superseded: bool = False,
        limit: int | None = None,
        believed_as_of: str | None = None,
        verified_only: bool = False,
        mode: str | None = None,
        target_ids: list[str] | None = None,
        offset: int = 0,
        known_at: datetime | None = None,
    ) -> dict[str, Any]:
        """The platform's own recent FINDINGS, with the critic-folded
        ``effective_confidence = min(confidence, critic_score)``.

        Reuses the substrate-reads ``list_findings`` shape (the finding<->critique
        LEFT JOIN LATERAL that surfaces the critic's ``overall_score``), dropping
        the FastAPI cursor/auth layer. Findings are analysis-derived (the
        platform's own synthesis), NOT raw signals — weigh accordingly.

        R1 / W2-T1 (read-truth): superseded findings are EXCLUDED by default
        (``superseded_by IS NULL``) so an agent reads the LIVE head of each
        finding chain, not a stale double-count. Pass ``include_superseded=True``
        to relax the gate (history/audit reads). This ONE handler serves
        consult + journal_read + deep_consult.

        EMPTY-TARGET RESOLUTION (2026-09-16 review, defect 3). The live consult
        asked this for ``situation_iran_war`` — an ACTIVE head target that
        ``list_targets`` had just offered it — and got zero rows, which reads
        as "the platform sees nothing there" when the truth is "no producer
        writes for that frame". An explicit ``target_id`` that matches nothing
        now resolves to its constituent desks and returns THEIR findings (each
        row still carries its own ``target_id``), or says plainly that the
        frame is empty. See :meth:`_resolve_empty_target_rows`.

        V3/P3 — ``believed_as_of`` (ISO-8601, DECISION time — the other
        clock from ``as_of``, spec §3.2): the findings Legba had published
        and not yet superseded on date D. ``analyst_outputs`` has no
        ``valid_*`` columns, so the predicate is
        ``produced_at <= D AND (superseded_at IS NULL OR superseded_at > D)``
        and the open-row ``superseded_by IS NULL`` gate does not apply (it
        is superseded TODAY — it was the live head on D). The critic fold
        also dates to D, so the answer carries the verdict Legba held at
        the time, not today's. A malformed value refuses.

        X3/G0 — ``verified_only`` (default off): only the findings a
        composition would ADMIT — the latest faithfulness verdict exists and
        ``min(confidence, verdict) >= LEGBA_COMPOSITION_VERIFY_FLOOR``, the
        same INNER-join rule (``legba.data.verified_findings``). The page limit
        applies AFTER the floor; ``critic_score`` is then the faithfulness
        verdict. The bake-off's arm C reads through it.

        W11-F150 — ``mode="survey"``: ONE row per target, its newest LIVE
        finding, ordered by target id, carrying a ``digest`` (at most 240
        characters from the top of the body) and ``body_chars``, never the
        body; drill with ``target_id`` or ``read_document(doc_id=<id>)``.
        Filters: ``target_id``
        / ``target_ids``, ``analyst_id``, ``severity``, ``since_hours``
        (default 168). It reads live heads only, so ``include_superseded``,
        ``believed_as_of`` and ``verified_only`` refuse. Both modes page:
        ``offset`` with ``rows_total`` (the true count, capped and flagged at
        2,000) and ``next_offset``. ``limit`` defaults to 20 rows (page) or 25
        targets (survey). See :mod:`.substrate_findings_reads`.

        E2 — ``known_at`` (evaluation harness only): ``created_at < K``; the
        live-head gate becomes the decision predicate at
        ``min(believed_as_of, K)``, the critic fold and ``since_hours`` date to
        it, survey mode refuses (:mod:`legba.data.raw_reader_known_at`).
        """
        read_mode = _findings.normalize_mode(mode)
        if read_mode is None:
            return {"rows": [], "refs": [], "count": 0, "error": (
                f"mode must be one of {list(_findings.LIST_FINDINGS_MODES)}, "
                f"got {mode!r}")}
        if read_mode == "survey":
            if known_at is not None:
                return {"rows": [], "refs": [], "count": 0, "mode": "survey",
                        "error": "survey mode reads today's live heads; this run "
                        "reads the record as of a fixed instant — use the default "
                        "page mode (list_findings(target_id=...))"}
            if include_superseded or believed_as_of is not None or verified_only:
                return {"rows": [], "refs": [], "count": 0, "mode": "survey",
                        "error": "survey mode reads the LIVE head of each target; "
                        "drop include_superseded / believed_as_of / verified_only, "
                        "or use the default page mode for them"}
            return await _findings.survey_findings(
                self._pool, target_id=target_id, target_ids=target_ids,
                analyst_id=analyst_id, severity=severity,
                since_hours=since_hours, limit=limit, offset=offset,
                max_row_limit=_MAX_ROW_LIMIT,
            )
        if limit is None:
            limit = _findings.PAGE_DEFAULT_LIMIT
        page_offset = _findings.clamp_offset(offset)
        try:
            believed_dt = (
                _temporal.parse_instant(believed_as_of, name="believed_as_of")
                if believed_as_of is not None else None
            )
        except _temporal.TemporalParameterError as exc:
            return {"rows": [], "refs": [], "error": str(exc)}
        if known_at is not None and (believed_dt is not None or not include_superseded):
            believed_dt = _record_cut.clamp(believed_dt, known_at)
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        clauses: list[str] = ["f.kind = 'finding'"]
        params: list[Any] = []
        believed_param: int | None = None
        if believed_dt is not None:
            params.append(believed_dt)
            believed_param = len(params)
            clauses.append(_temporal.decision_predicate("f", believed_param))
        elif not include_superseded:
            clauses.append("f.superseded_by IS NULL")
        if known_at is not None:
            params.append(known_at)
            clauses.append(_record_cut.created_before("f.created_at", len(params)))
            believed_param = believed_param or len(params)  # dates the critic fold
        target_param: int | None = None
        targets = _findings.target_list(target_id, target_ids)
        if targets:
            params.append(targets)
            target_param = len(params)
            clauses.append(f"f.target_id = ANY(${len(params)}::text[])")
        if analyst_id is not None:
            params.append(analyst_id)
            clauses.append(f"f.analyst_id = ${len(params)}")
        if severity is not None:
            params.append(severity)
            clauses.append(f"f.severity = ${len(params)}")
        if since_hours is not None:
            params.append((known_at or datetime.now(timezone.utc)) - timedelta(hours=int(since_hours)))
            clauses.append(f"f.produced_at >= ${len(params)}")
        where_params = list(params)
        params.append(clamped_limit)
        # believed_as_of dates the critic fold too: the verdict Legba HELD
        # on D (the latest critique produced by then), not today's.
        critic_as_of = (
            f"AND cr.produced_at <= ${believed_param}"
            if believed_param is not None else ""
        )
        limit_param = len(params)
        if verified_only:
            params.append(_verified.verify_floor())
        offset_param: int | None = None
        if page_offset:
            params.append(page_offset)
            offset_param = len(params)
        if verified_only:
            sql = _verified.verified_findings_sql(
                " AND ".join(clauses), limit_param, limit_param + 1,
                critic_as_of=critic_as_of, offset_param=offset_param,
            )
        else:
            sql = critic_folded_findings_sql(
                " AND ".join(clauses), limit_param, critic_as_of=critic_as_of,
                offset_param=offset_param,
            )
        resolution: dict[str, Any] | None = None
        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql, *params)
            if (not records and target_param is not None and len(targets) == 1
                    and not page_offset):
                records, resolution = await widen_to_member_targets(
                    conn, sql, params, target_param, targets[0],
                )
                if resolution.get("members"):
                    where_params[target_param - 1] = resolution["members"]
            # The verified floor is a join, not a predicate on f, so its
            # count runs through the same SQL, unpaged, up to the cap.
            count_sql = (
                _verified.verified_findings_sql(
                    " AND ".join(clauses), len(where_params) + 1,
                    len(where_params) + 2, critic_as_of=critic_as_of,
                ) if verified_only else None
            )
            paging = await _findings.page_rows_total(
                conn, " AND ".join(clauses), where_params,
                offset=page_offset, count=len(records), limit=clamped_limit,
                count_sql=count_sql,
                count_tail=[params[limit_param]] if verified_only else [],
            )

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
        out: dict[str, Any] = {
            "rows": rows, "refs": refs, "count": len(rows), **paging,
        }
        if believed_dt is not None:
            out["believed_as_of"] = believed_dt.isoformat()
        if verified_only:
            out["verified_only"] = {"floor": params[limit_param]}
        stamp_target_resolution(
            out, targets[0] if targets else str(target_id), resolution,
        )
        if known_at is not None:
            return _record_cut.stamp(out, known_at, how="analyst_outputs.created_at < K; heads and critic fold at K")
        return out

    async def list_situations(
        self,
        *,
        status: str | None = None,
        target_id: str | None = None,
        since_hours: int | None = None,
        limit: int = 20,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """First-class ``situations`` (the platform's clustered ongoing frames).

        Analysis-derived (clustered from findings), not operator-vetted ground
        truth. Pass a returned ``situation_id`` to ``query_hypotheses`` to pull
        the ACH rows hanging off a situation.

        Ordered by ``intensity_score`` then ``last_event_at``, so a ``limit``
        is the TOP frames rather than an arbitrary slice — ``updated_at``, the
        previous key, is stamped on every row by the hourly re-cluster and
        ordered by nothing at all. An empty read carries ``why_empty`` naming
        the filters applied, and names a ``status`` outside the live
        vocabulary as the reason when that is what emptied it.

        V3/P3 — ``as_of`` (ISO-8601, validity time) adds the canonical as-of
        predicate: the frames that held on date D, INCLUDING frames that have
        closed since. There is no open-row gate on this read (closed frames
        are already returned), so ``as_of`` narrows rather than swaps —
        ``unbounded_start`` counts returned frames with no recorded
        ``valid_from``. A malformed value refuses.

        V3/P6 — the body lives in ``substrate_frame_reads.list_situations``
        (the module-size extraction); this method is the Protocol surface.
        """
        return await _frames.list_situations(
            self._pool,
            status=status,
            target_id=target_id,
            since_hours=since_hours,
            limit=limit,
            as_of=as_of,
            max_row_limit=_MAX_ROW_LIMIT,
        )

    async def query_predictions(
        self,
        *,
        target_id: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """The platform's event-volume forecasts (analyst_outputs kind='prediction').

        STORED SHAPE (W2-T6 / M3): the emit path
        (``actor_payload._PAYLOAD_SELECTORS[OutputKind.PREDICTION]``) UNWRAPS
        the analyst-side ``finding.data["prediction"]`` blob, so the stored
        row's ``data`` IS the PredictionPayload dump at the TOP level
        (point_estimate / ci_lower / ci_upper / horizon_days / method /
        narrative / status). The old nested ``data->'prediction'`` read path
        matched ZERO live rows and was deleted — top-level is canonical.
        ``forecast_method`` (the writer's ``method`` extra) of ``naive_mean``
        ⇒ no trend could be fit (weak prior); ``auto_arima`` ⇒ a model was
        fitted.  The resolver (calibration_tracking) later merges the
        lifecycle ``status`` / ``resolved_outcome`` at the same top level via
        jsonb ``||``.
        Title/body columns are empty on prediction rows by design — read the blob.

        FEED HONESTY: the prediction feed FROZE on 2026-07-01 when
        country_predictor was retired — rows are HISTORICAL forecasts, not a
        live product. The response carries ``latest_produced_at`` +
        ``feed_note`` so readers can state that instead of serving frozen
        rows as fresh.
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        clauses: list[str] = ["kind = 'prediction'"]
        params: list[Any] = []
        if target_id is not None:
            params.append(target_id)
            clauses.append(f"target_id = ${len(params)}")
        if status is not None:
            params.append(status)
            # Both the predictor's initial 'open' (top-level after the emit
            # unwrap) and the resolver's later resolved/refuted merge live at
            # data->>'status' — one canonical path.
            clauses.append(f"data->>'status' = ${len(params)}")
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
        latest_produced_at: datetime | None = None
        for r in records:
            refs.append(str(r["id"]))
            raw = r["data"]
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
            # Canonical stored shape: the PredictionPayload dump at the TOP
            # level of ``data`` (the emit path unwraps the nested blob — see
            # the docstring). The dead ``data['prediction']`` branch is gone.
            pred = data if isinstance(data, dict) else {}
            produced = r["produced_at"]
            if isinstance(produced, datetime) and (
                latest_produced_at is None or produced > latest_produced_at
            ):
                latest_produced_at = produced
            rows.append({
                "id": str(r["id"]),
                "target_id": r["target_id"],
                "analyst_id": r["analyst_id"],
                "produced_at": produced.isoformat()
                    if isinstance(produced, datetime) else None,
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
                # Lifecycle status + outcome: initial 'open' from the writer
                # and the resolver's later merge both live at the top level.
                "status": pred.get("status"),
                "resolved_outcome": pred.get("resolved_outcome"),
            })
        return {
            "rows": rows,
            "refs": refs,
            "count": len(rows),
            # Feed honesty (W2-T6): the writer retired 2026-07-01 — say so
            # instead of letting frozen rows read as a live forecast product.
            "latest_produced_at": latest_produced_at.isoformat()
                if latest_produced_at is not None else None,
            "feed_note": (
                "prediction feed FROZEN since 2026-07-01 (country_predictor "
                "retired) — rows are historical forecasts, not live output; "
                "check latest_produced_at before treating any row as current"
            ),
        }

    # ------------------------------------------------------------------
    # belief_as_of (V3/P3) — "what did Legba believe on date D"
    # ------------------------------------------------------------------

    async def belief_as_of(
        self,
        *,
        as_of: str,
        target_id: str | None = None,
        fold_verdicts: str = "as_of",
        limit: int = 20,
        verified_only: bool = False,
        known_at: datetime | None = None,
    ) -> dict[str, Any]:
        """The findings Legba had published and not yet superseded on date D
        (DATA MODEL V3 §3.4) — a DECISION-time read on ``analyst_outputs``.

        E2 — ``known_at`` (evaluation harness only): ``created_at < K`` and D
        clamped to ``min(as_of, K)``; ``fold_verdicts='latest'`` refuses.

        ``analyst_outputs`` has no ``valid_*`` columns (§3.1's correction):
        its validity interval is ``[produced_at, superseded_at)``, which is
        what the decision predicate filters. ``as_of`` is REQUIRED — a
        belief-as-of read with no date is a different tool (``list_findings``).

        The faithfulness VERDICT folds two ways, and the response says which:

        * ``fold_verdicts="as_of"`` (default) — the verdict Legba HELD on D
          (the latest faithfulness critique produced by then). A finding whose
          verdict had not yet landed on D comes back
          ``effective_confidence=None`` and counts toward
          ``verdict_pending_at_as_of`` — the honest "not yet graded" answer.
        * ``fold_verdicts="latest"`` — today's verdict for each finding, for
          "what we believe NOW about what we said THEN".

        The fold NEVER pools into a single score — each row carries its own
        ``effective_confidence`` and the envelope carries none.

        X3/G0 — ``verified_only`` (default off): only findings ADMISSIBLE under
        the chosen fold's verdict (``legba.data.verified_findings``); a row
        pending at D is not admissible under ``as_of`` and is excluded.
        """
        try:
            as_of_dt = _temporal.parse_instant(as_of, name="as_of")
        except _temporal.TemporalParameterError as exc:
            return {"rows": [], "refs": [], "error": str(exc)}
        fold = (fold_verdicts or "as_of").strip().lower()
        if fold not in ("as_of", "latest"):
            return {
                "rows": [], "refs": [],
                "error": (
                    f"fold_verdicts must be 'as_of' or 'latest', "
                    f"got {fold_verdicts!r}"
                ),
            }
        if known_at is not None:
            if fold == "latest":
                return {"rows": [], "refs": [], "error": (
                    "fold_verdicts='latest' folds today's verdict; this run reads "
                    "the record as of a fixed instant — use fold_verdicts='as_of'")}
            as_of_dt = _record_cut.clamp(as_of_dt, known_at)
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))

        clauses: list[str] = [
            "f.kind = 'finding'",
            _temporal.decision_predicate("f", 1),
        ]
        params: list[Any] = [as_of_dt]
        if known_at is not None:
            params.append(known_at)
            clauses.append(_record_cut.created_before("f.created_at", len(params)))
        if target_id is not None:
            params.append(target_id)
            clauses.append(f"f.target_id = ${len(params)}")
        params.append(clamped_limit)

        # H17 — the two verdict folds are SET-BASED and shared with
        # `/v3/belief`, which asks the identical question (see
        # `legba.data.critic_fold.dual_verdict_findings_sql`). Both are pinned
        # to `title LIKE 'Faithfulness verify%'` so a generic critique can never
        # win the produced_at race and mask the verify verdict.
        if verified_only:
            params.append(_verified.verify_floor())
            sql = _verified.verified_belief_sql(
                " AND ".join(clauses), len(params) - 1, len(params),
                fold_verdicts=fold,
            )
        else:
            sql = _critic_fold.dual_verdict_findings_sql(
                " AND ".join(clauses), len(params),
            )

        async with self._pool.acquire() as conn:
            records = await conn.fetch(sql, *params)

        rows: list[dict[str, Any]] = []
        refs: list[str] = []
        pending = 0
        for r in records:
            refs.append(str(r["id"]))
            confidence = (
                float(r["confidence"]) if r["confidence"] is not None else None
            )
            score_key = (
                "verdict_score_as_of" if fold == "as_of"
                else "verdict_score_latest"
            )
            at_key = "verdict_at_as_of" if fold == "as_of" else "verdict_at_latest"
            verdict_score = (
                float(r[score_key]) if r[score_key] is not None else None
            )
            verdict_at = r[at_key]
            # verdict_pending_at_as_of is always about DATE D regardless of
            # fold: did a verdict exist by then. Under fold='latest' a row
            # graded today still reports that it was ungraded at D.
            row_pending = r["verdict_score_as_of"] is None
            if row_pending:
                pending += 1
            rows.append({
                "id": str(r["id"]),
                "title": r["title"],
                "body": (r["body"] or "")[:2000],
                "confidence": confidence,
                "severity": r["severity"],
                "target_id": r["target_id"],
                "analyst_id": r["analyst_id"],
                "produced_at": r["produced_at"].isoformat()
                    if isinstance(r["produced_at"], datetime) else None,
                "verdict_score": verdict_score,
                "verdict_at": verdict_at.isoformat()
                    if isinstance(verdict_at, datetime) else None,
                # min(confidence, verdict) per the findings fold — NULL when
                # the chosen fold has no verdict (pending), never pooled.
                "effective_confidence": (
                    min(confidence, verdict_score)
                    if confidence is not None and verdict_score is not None
                    else None
                ),
                "verdict_pending_at_as_of": row_pending,
            })

        out = {
            "rows": rows,
            "refs": refs,
            "count": len(rows),
            "as_of": as_of_dt.isoformat(),
            "target_id": target_id,
            # Which verdict clock the scores came from — stamped so a reader
            # cannot mistake a latest-fold answer for an as-of one.
            "fold_verdicts": fold,
            "verdict_pending_at_as_of": pending,
            **({"verified_only": {"floor": params[-1]}} if verified_only else {}),
            "note": (
                "rows carry their own effective_confidence; no pooled score "
                "is computed — a belief-as-of read is a register, not a number"
            ),
        }
        if known_at is not None:
            return _record_cut.stamp(out, known_at, how="analyst_outputs.created_at < K; as_of clamped to K")
        return out

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
        known_at: datetime | None = None,
    ) -> dict[str, Any]:
        """The ingest sources and their freshness/coverage.

        Joins each head source descriptor to its most-recent signal time and its
        most-recent poll outcome (``source_poll_outcomes``:
        'success'|'empty'|'error' rollup — migration 0046, success added by
        0114). Use to qualify a 'no signal on X' answer — no coverage vs a quiet
        feed. ``silent_only`` filters to sources silent > ``silent_hours``.

        W11-F152 -- the body lives in ``substrate_source_health.list_sources``:
        freshest-first rows behind a whole-fleet ``summary`` (staleness and
        last-poll-outcome counts, an attention roster), each row carrying
        ``last_success_at`` and the ``staleness`` verdict; ``empty`` is
        documented as "no new items on the latest poll", not dead.
        """
        return await _source_health.list_sources(
            self._pool,
            active_only=active_only,
            silent_only=silent_only,
            silent_hours=silent_hours,
            known_at=known_at,
        )

    # ------------------------------------------------------------------
    # list_scheduled_events (W11-F141) — the always-on calendar
    # ------------------------------------------------------------------

    async def list_scheduled_events(
        self,
        *,
        window_start: str | None = None,
        window_end: str | None = None,
        scope: str | None = None,
        subject_keys: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """The scheduled-events calendar (migration 0240): FUTURE dated things
        read by overlap over a window (default now → +14 days) with a
        ``coverage`` block that tells "none held" from "calendar empty". See
        :mod:`.substrate_calendar_reads` (the body lives there)."""
        return await _calendar.list_scheduled_events(
            self._pool, window_start=window_start, window_end=window_end,
            scope=scope, subject_keys=subject_keys, limit=limit,
            max_row_limit=_MAX_ROW_LIMIT,
        )

    # ------------------------------------------------------------------
    # get_timeline (S4-T6)
    # ------------------------------------------------------------------

    async def get_timeline(
        self,
        *,
        subject: str,
        limit: int = 40,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Time-ordered merge of current facts + recent signals on a subject.

        Builds one chronological view of what the substrate holds about a
        subject by merging two streams:

          * **facts** — current rows (the migration-0032 open pair plus the
            P7 ``origin_class`` leg, ``live_gate_sql``) whose ``subject``
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

        V3/P3 — ``since``/``until`` (ISO-8601) bound the window on each
        stream's ANCHOR, half-open ``[since, until)``, applied in SQL so the
        per-stream cap cannot evict an in-window item with an out-of-window
        one. Either bound may be given alone; a malformed value refuses
        rather than widening to all-time.

        V3/P6 — the body lives in ``substrate_frame_reads.get_timeline``
        (the module-size extraction); this method is the Protocol surface.
        """
        return await _frames.get_timeline(
            self._pool,
            subject=subject,
            limit=limit,
            since=since,
            until=until,
            max_row_limit=_MAX_ROW_LIMIT,
        )

    # ------------------------------------------------------------------
    # query_events / inspect_event (V3/P6 — spec §6.1)
    # ------------------------------------------------------------------

    async def query_events(
        self,
        *,
        target_id: str | None = None,
        geo: str | list[str] | None = None,
        category: str | None = None,
        lifecycle_state: str | None = None,
        entity: str | None = None,
        since: str | None = None,
        until: str | None = None,
        as_of: str | None = None,
        include_origin: list[str] | None = None,
        situation_id: str | None = None,
        limit: int = 20,
        known_at: datetime | None = None,
    ) -> dict[str, Any]:
        """The filtered event list (the V3 ``events`` table, migration 0202).

        ``target_id`` / ``category`` / ``lifecycle_state`` (one of the five-
        state vocabulary — an unknown state refuses loud) / ``geo`` (ISO2
        code or list, array-overlap) / ``entity`` (canonical-name substring
        over ``event_entity_links``) / ``situation_id`` (the tracked-events
        read — events a situation links to) are optional filters.

        ``since``/``until`` (ISO-8601) bound the event's OCCURRENCE span by
        overlap — an event that began before ``until`` and had not ended
        before ``since`` is in the window; ``time_start``/``time_end`` NULLs
        fall back to ``produced_at``. A malformed value refuses rather than
        widening to all-time.

        The default (no ``as_of``) read is the OPEN gate — the 0032 pair
        plus the P7 ``origin_class`` leg — so a backfilled event can never
        read as live. ``as_of`` swaps to the validity predicate and only
        there does ``include_origin`` widen the class set (default
        ``LIVE_CLASSES``; an unknown class refuses). ``unbounded_start``
        counts returned rows with no recorded ``valid_from``.

        The body lives in ``substrate_frame_reads.query_events`` (``known_at``:
        lane E2's record-time cut, evaluation harness only).
        """
        return await _frames.query_events(
            self._pool,
            target_id=target_id,
            geo=geo,
            category=category,
            lifecycle_state=lifecycle_state,
            entity=entity,
            since=since,
            until=until,
            as_of=as_of,
            include_origin=include_origin,
            situation_id=situation_id,
            limit=limit,
            max_row_limit=_MAX_ROW_LIMIT,
            **({"known_at": known_at} if known_at is not None else {}),
        )

    async def inspect_event(
        self,
        *,
        event_id: str,
        known_at: datetime | None = None,
    ) -> dict[str, Any]:
        """The one-event dossier (V3/P6 — spec §6.1).

        Returns ``found=True`` plus the five sections: the ``event`` row
        itself (full provenance + geo + lifecycle columns); ``signals`` —
        its ranked evidence (``signal_event_links`` joined to signal titles,
        relevance-ordered); ``actors`` — entities with roles
        (``event_entity_links`` → ``entity_profiles``); ``edges`` — its
        event edges in BOTH directions (superseded edges excluded);
        ``situations`` — the frames tracking it (``situation_event_links``);
        and ``lifecycle`` — the append-only ledger OLDEST→NEWEST (the
        ``opened`` row first). ``refs`` unions every substrate id so the
        consult loop can cite them.

        A non-uuid ``event_id`` or a missing row returns ``found=False``
        with a named error — never an empty-looking success.

        The body lives in ``substrate_frame_reads.inspect_event`` (``known_at``:
        lane E2's record-time cut, evaluation harness only).
        """
        if known_at is not None:
            return await _frames.inspect_event(self._pool, event_id=event_id, known_at=known_at)
        return await _frames.inspect_event(self._pool, event_id=event_id)

    # ------------------------------------------------------------------
    # The COLLECTION series reads (7g-2 §6)
    #
    # History, not now. These two are the only readers of `observations`,
    # the bitemporal series store a COLLECTION loads once and nothing
    # schedules. They are read-only, publish nothing, and read only
    # holdings whose descriptor head is in state `loaded` — the operator's
    # approval is the gate, and it is enforced in the leaf rather than
    # trusted to a caller.
    #
    # The bodies live in ``_observations_read``.
    # ------------------------------------------------------------------

    async def series_history(
        self,
        *,
        series_id: str,
        subject: str,
        since: Any = None,
        until: Any = None,
        as_of: Any = None,
        collection_id: str | None = None,
        limit: int = _observations.MAX_SERIES_ROWS,
        known_at: datetime | None = None,
    ) -> dict[str, Any]:
        """One curated HISTORICAL series for one subject, over a valid window.

        ``since``/``until`` bound the VALID time — the period each number is
        ABOUT — and are required: a series read is always over a bounded
        window, never all-time, and a malformed bound refuses rather than
        widening. ``as_of`` is the RECORD time: with it, each period comes
        back as the latest revision the provider had published on or before
        that instant (so an as-of replay cannot inherit a restatement that
        had not happened yet); without it, the latest revision on record.

        Every row carries its ``valid_from``/``valid_to``, its
        ``record_time``, the value with its ``unit``, the ``source_url`` and
        the ``sha256`` of the file the number was read out of, and a
        ``ref`` (``observation:<uuid>``) the citation builder resolves.

        E2 — ``known_at`` (evaluation harness only): ``loaded_at < K`` and
        ``as_of`` clamped to K.
        """
        return await _observations.read_series_history(
            self._pool,
            series_id=series_id,
            subject=subject,
            since=since,
            until=until,
            as_of=as_of,
            collection_id=collection_id,
            limit=limit,
            **({"known_at": known_at} if known_at is not None else {}),
        )

    async def series_compare(
        self,
        *,
        series_id: str,
        subjects: list[str] | None = None,
        since: Any = None,
        until: Any = None,
        as_of: Any = None,
        collection_id: str | None = None,
        limit: int = _observations.MAX_SERIES_ROWS,
        known_at: datetime | None = None,
    ) -> dict[str, Any]:
        """The same series across several subjects over the same window.

        Same bitemporal rule and same row shape as :meth:`series_history`, in
        ONE statement rather than N calls. A subject the holding does not
        carry contributes no rows and is named in ``subjects_with_no_rows`` —
        never padded with a zero nobody published.
        """
        return await _observations.read_series_compare(
            self._pool,
            series_id=series_id,
            subjects=subjects or [],
            since=since,
            until=until,
            as_of=as_of,
            collection_id=collection_id,
            limit=limit,
            **({"known_at": known_at} if known_at is not None else {}),
        )

    async def live_series(
        self,
        *,
        series_id: str | None = None,
        subject: str | None = None,
        source_id: str | None = None,
        since: Any = None,
        until: Any = None,
        limit: int = _live_obs.DEFAULT_ROWS,
        known_at: datetime | None = None,
    ) -> dict[str, Any]:
        """The LIVE numeric series (``origin_class='live'``, ``collection_id
        'live:<source_id>'``) — a separate reader from the history tools, which
        never read a live row (SEAMS #57/#62).

        With no ``series_id`` it lists the live series held (id, subject, unit,
        period, newest observation, source). With ``series_id`` and ``subject``
        it reads that series over a bounded valid window, every revision with
        its two timestamps (``record_time``, ``loaded_at``), each row labelled
        ``live`` with its source id. Body and bounds in
        :mod:`legba.runtime._observations_live_read`; ``known_at`` (lane E2)
        keeps rows loaded and recorded before K.
        """
        return await _live_obs.read_live_series(
            self._pool, series_id=series_id, subject=subject, source_id=source_id,
            since=since, until=until, limit=limit, known_at=known_at,
        )

    async def series_holdings(self, *, limit: int = 200) -> dict[str, Any]:
        """WHAT curated history exists at all — the catalogue, not the numbers.

        One line per series across every LOADED holding, with the subjects
        that series carries. The consult prompt renders this at the start of
        a run so the planner knows the series tools have something to read
        BEFORE it decides to answer a historical question from memory; the
        review found ``series_history`` / ``series_compare`` registered,
        granted, on the wire and invoked zero times, because nothing ever
        told the model the holdings existed.

        ``series: []`` with ``series_count: 0`` means no collection is
        loaded. That is an answer, and the renderer prints it as one.
        """
        async with self._pool.acquire() as conn:
            series = await _observations.series_holdings(conn, limit=limit)
        subjects = sorted({s for row in series for s in row.get("subjects", [])})
        return {
            "series": series,
            "series_count": len(series),
            "subjects": subjects,
            "count": len(series),
        }

    async def classify_cited_refs(self, *, refs: list[str]) -> dict[str, Any]:
        """What a consult answer's CITED ids actually are — by class and by kind.

        7g-2's provenance census read. The body lives in
        ``substrate_cited_refs.classify_cited_refs`` (split out in W4b F-9 at
        the size ceiling, when it gained the ``events`` fold and the
        :data:`CITED_REFS_CLASSIFY_CAP` bound); this method is the port's
        surface for it and keeps the signature every caller binds.
        """
        return await _cited_refs.classify_cited_refs(self._pool, refs=refs)

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
        material: current facts (the 0032 open pair plus the P7
        ``origin_class`` leg), open nexuses (``valid_until IS NULL AND
        superseded_by IS NULL``), the hypothesis status mix, and a handful of
        recent
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
                    f"""
                    SELECT count(*) FROM facts
                    WHERE target_id = $1
                      AND {_origin.live_gate_sql("")}
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
            # The consult trace probes ``count`` first and this read never
            # carried one, so every compare_targets call in the ledger reads
            # as count 0 — a six-target comparison included (2026-09-28).
            "count": len(targets),
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
        families: list[str] | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Ranked signed PATHS from ``subject`` to ``obj`` over open edges.

        Walks the OPEN ``entity_edges`` graph (``valid_until IS NULL AND
        superseded_by IS NULL``) with a recursive CTE, treating each open edge
        as a directed ``src → dst`` edge (the ``intermediary`` cut-out, if
        present, is a property of the edge, not a separate node) carrying a
        POLARITY sign (+1 / -1 / 0).  Returns paths of 1..``max_hops`` hops,
        each with the running **polarity product** — the structural-balance
        sign of the whole chain (an even number of -1 edges → +1 net
        "the enemy of my enemy"; odd → -1).

        The graph is CYCLIC, so each branch carries the path-so-far as a
        ``uuid[]`` of node ids; a candidate next hop already on the path is
        pruned (the VISITED-SET guard) so traversal terminates with no reliance
        on ``max_hops``.  ``max_hops`` is clamped to the walk module's hop
        ceiling; the frontier and the returned set are clamped to its row
        cap (``substrate_graph_walks``).

        When ``polarity_product`` (∈ {-1, 0, 1}) is supplied, only paths whose
        net sign matches are returned.  The filter is applied IN SQL, before
        the ``LIMIT`` cutoff (W2-T6 / M5): a Python-side post-filter silently
        dropped matching paths ranking past the fetch.  Paths are ranked
        shortest-first, then by descending min-confidence (the weakest link).

        W3-A — CUT OVER FROM ``nexuses``, AND MADE FAMILY-AWARE. Two changes,
        and the second matters more than the store swap:

        * **Identity.** The walk was keyed on ``lower(subject)`` =
          ``lower(object)`` text joins, so a merge silently severed a chain
          (the two halves named the loser and the keeper) and one name borne by
          two profiles fused two actors into one hop. It now joins on
          ``src_id``/``dst_id`` foreign keys and hits
          ``idx_entity_edges_out``. ``nodes`` still returns display names for
          every existing consumer; ``node_ids`` carries the identity used.
        * **A co-mention is not a path.** 8,635 of 12,732 open nexus rows are
          ``co occurs with``, so the old walk's chains were overwhelmingly
          "these two nouns appeared in the same document, twice in a row" —
          the co-mention hairball presented as tradecraft. ``families``
          defaults to :data:`_ASSERTING_FAMILIES` (``relation`` +
          ``reference``); pass ``["cooccurrence"]`` explicitly to walk it.

        An endpoint that resolves to no entity now returns a ``warnings`` entry
        instead of an empty ``paths`` list, because on an id-keyed walk the two
        are otherwise indistinguishable.

        V3/P3 — ``as_of`` (ISO-8601, validity time) walks the graph AS IT
        STOOD on date D: the canonical as-of predicate replaces the open-row
        gate per hop, on BOTH the seed and the recursive arm (a path through
        an edge closed since D is not a path that existed on D). The envelope
        then carries ``unbounded_start`` over the walked edges.
        """
        try:
            as_of_dt = (
                _temporal.parse_instant(as_of, name="as_of")
                if as_of is not None else None
            )
        except _temporal.TemporalParameterError as exc:
            return {
                "subject": subject, "object": obj,
                "paths": [], "refs": [], "error": str(exc),
            }
        return await _walks.query_paths(
            self._pool,
            subject=subject,
            obj=obj,
            max_hops=max_hops,
            polarity_product=polarity_product,
            limit=limit,
            families=families,
            as_of=as_of_dt,
        )

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
        families: list[str] | None = None,
        as_of: str | None = None,
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

        V3/P3 — ``as_of`` inherits :meth:`query_paths`'s per-hop temporal
        gate exactly.
        """
        try:
            as_of_dt = (
                _temporal.parse_instant(as_of, name="as_of")
                if as_of is not None else None
            )
        except _temporal.TemporalParameterError as exc:
            return {
                "subject": subject, "object": obj,
                "chains": [], "refs": [], "error": str(exc),
            }
        return await _walks.find_proxy_chains(
            self._pool,
            subject=subject,
            obj=obj,
            max_hops=max_hops,
            polarity_product=polarity_product,
            limit=limit,
            families=families,
            as_of=as_of_dt,
        )

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
        families: list[str] | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Entities that SIT ON paths between two entity sets (brokers).

        A broker is an intermediate node that lies on a path from some member
        of ``camp_a`` to some member of ``camp_b`` (and is itself in neither
        camp).  Walks the open ``entity_edges`` graph from every ``camp_a``
        seed (same recursive CTE + VISITED-SET guard + hop cap as
        :meth:`query_paths`); for every walk that terminates on a ``camp_b``
        member, the INTERIOR nodes of the path are credited as brokers.
        Brokers are ranked by how many distinct A→B paths run through them
        (betweenness-flavored degree), then named.

        Both camps are clamped to the walk module's camp cap and the result
        to its broker cap (``substrate_graph_walks``).

        W3-A — CUT OVER FROM ``nexuses``. The tally key is the entity ID, so
        two surfaces of one actor (an alias, or a name the GC has since merged)
        can no longer be counted as two separate brokers, each with half the
        path count — the specific error a brokerage RANKING must not make,
        since it demotes the very node the measure exists to find.  Family
        semantics are :meth:`query_paths`'s: a broker sitting on a chain of
        co-mentions brokers nothing, so ``cooccurrence`` is off by default.

        A camp member that resolves to no entity is reported in ``warnings``
        and excluded, never silently dropped: a ranking over half a camp is a
        different answer from one over all of it.

        V3/P3 — ``as_of`` (ISO-8601, validity time) applies the canonical
        as-of predicate per hop on BOTH the seed and the recursive arm, so
        the brokerage ranking is computed over the graph as it stood on
        date D. The envelope then carries ``unbounded_start``.
        """
        try:
            as_of_dt = (
                _temporal.parse_instant(as_of, name="as_of")
                if as_of is not None else None
            )
        except _temporal.TemporalParameterError as exc:
            return {
                "camp_a": camp_a, "camp_b": camp_b,
                "brokers": [], "refs": [], "error": str(exc),
            }
        return await _walks.query_brokers(
            self._pool,
            camp_a=camp_a,
            camp_b=camp_b,
            max_hops=max_hops,
            limit=limit,
            families=families,
            as_of=as_of_dt,
        )

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
        # B-8 — a NARROWED read that names a non-producer must not answer with a
        # bare empty. On 2026-08-03 the journal's planner asked for
        # ``analyst_id='country_assessor'`` — a state='draft' analyst that has
        # produced nothing for months — got `rows: []`, and narrated it as "the
        # assessment engine produced no country-level rows in the last 48 hours"
        # on a day with 1,562 successful runs and 131 fresh country_composition
        # findings. The read was CORRECT and the sentence was FALSE, because an
        # empty answer to the wrong question is indistinguishable from an empty
        # engine unless the tool says so. Now it says so, and says what to ask
        # instead — the narrator can recover inside the same gather loop.
        if analyst_id is not None and analyst_id not in _ASSESSMENT_PRODUCER_ANALYSTS:
            live = ", ".join(_ASSESSMENT_PRODUCER_ANALYSTS)
            return {
                "rows": [],
                "refs": [],
                "count": 0,
                "disagreements": None,
                "unavailable": (
                    f"'{analyst_id}' is not a live assessment producer, so this "
                    f"empty result says NOTHING about whether the engine is "
                    f"producing — it only says you asked for an analyst that "
                    f"writes nothing today. Live producers: {live}. Re-read with "
                    f"no analyst_id to see the whole live assessment surface."
                ),
            }
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
        sql = critic_folded_findings_sql(" AND ".join(clauses), len(params))
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
        # H-2 (audit W6 / B0-5) — reconcile the returned countries' scorecards
        # against their CURRENT country_composition heads and surface any
        # REMAINING divergence as a bounded `disagreements` block, using the SAME
        # pure reducers the registry's /eval/country_scorecard endpoint uses
        # (data/registry/scorecard_reconcile). This is a READ-TIME reflection
        # surface so the journal can narrate the divergence honestly (the two P4
        # products judge over different windows) — it is NEVER a gate and NEVER
        # computed at compose-write. Fully fail-safe: any error (incl. the import)
        # degrades to `disagreements: null` and never breaks the assessments read.
        #
        # B-8: `null` and `[]` are DIFFERENT answers and are now kept different.
        # `[]` means "measured, and the two products agree". `null` means "not
        # measured" — no countries in this read to reconcile, or the reconcile
        # itself failed. Collapsing them is how one bad `analyst_id` produced BOTH
        # halves of the 08-03 false claim: the disagreement scan derives its
        # targets FROM the returned rows, so an empty read short-circuited it to
        # `[]`, and `[]` read as "no disagreements exist" rather than "nothing was
        # compared".
        disagreements = await self._reconcile_scorecard_disagreements(rows, refs)
        return {
            "rows": rows,
            "refs": refs,
            "count": len(rows),
            "disagreements": disagreements,
        }

    async def _reconcile_scorecard_disagreements(
        self,
        rows: list[dict[str, Any]],
        refs: list[str],
    ) -> list[dict[str, Any]] | None:
        """Reconcile the assessment countries' scorecards vs their live
        ``country_composition`` heads (H-2 / B0-5). Returns a bounded list of
        divergence rows (each a ``ScorecardDisagreement`` dump + ``target_id``);
        appends the contested finding ids to ``refs`` (deduped) so the journal
        may cite them. Fully fail-safe — never raises.

        B-8 — the return is TRISTATE, deliberately:

          * a non-empty list — measured, and these are the divergences;
          * ``[]``          — measured, and the two products agree;
          * ``None``        — NOT measured (no countries in this read to
                              reconcile, or the reconcile failed).

        The last case used to be ``[]`` as well, which is how one wrong
        ``analyst_id`` on 08-03 produced both halves of a false claim: this scan
        derives its targets FROM the rows above it, so an empty read
        short-circuited it, and the caller could not tell "they agree" from
        "nothing was compared". An absence must never render as a finding of
        agreement.
        """
        try:
            from ..data.registry.scorecard_reconcile import (
                composition_usages,
                scorecard_disagreements,
            )

            country_targets = sorted({
                str(r["target_id"]) for r in rows
                if r.get("target_id") and str(r["target_id"]) != "world"
            })[:_DISAGREEMENT_MAX_TARGETS]
            if not country_targets:
                return None  # nothing to compare — NOT "they agree"
            async with self._pool.acquire() as conn:
                # Latest scorecard head per target — bands.dimensions carries each
                # dimension's band + reason (insufficient-evidence = excluded).
                sc_rows = await conn.fetch(
                    """
                    SELECT DISTINCT ON (target_id) target_id, data
                      FROM analyst_outputs
                     WHERE kind = 'scorecard'
                       AND superseded_by IS NULL
                       AND target_id = ANY($1::text[])
                     ORDER BY target_id, produced_at DESC, id DESC
                    """,
                    country_targets,
                )
                # Latest country_composition head per target — citations
                # (data.data.citations) + the derived_from uuid[] COLUMN.
                comp_rows = await conn.fetch(
                    """
                    SELECT DISTINCT ON (target_id)
                           target_id, derived_from::text[] AS derived_from, data
                      FROM analyst_outputs
                     WHERE kind = 'finding'
                       AND analyst_id = 'country_composition'
                       AND superseded_by IS NULL
                       AND target_id = ANY($1::text[])
                     ORDER BY target_id, produced_at DESC, id DESC
                    """,
                    country_targets,
                )
                dims_by_target: dict[str, dict[str, Any]] = {}
                for scr in sc_rows:
                    raw = scr["data"]
                    payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    # A malformed (non-dict) scorecard `data` must degrade to {}
                    # and SKIP just that row — not raise and (via the outer except)
                    # sink the whole disagreements block. Mirrors the composition
                    # parse guard below (review: fail-safe consistency).
                    payload = payload if isinstance(payload, dict) else {}
                    bands = ((payload.get("data") or {}).get("bands")) or {}
                    dims = bands.get("dimensions")
                    if isinstance(dims, dict):
                        dims_by_target[str(scr["target_id"])] = dims
                comp_by_target: dict[str, tuple[Any, list[str]]] = {}
                unresolved: set[str] = set()
                for cr in comp_rows:
                    raw = cr["data"]
                    payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
                    payload = payload if isinstance(payload, dict) else {}
                    citations = (payload.get("data") or {}).get("citations")
                    derived = [str(x) for x in (cr["derived_from"] or [])]
                    comp_by_target[str(cr["target_id"])] = (citations, derived)
                    cited = composition_usages(citations, [], {})
                    unresolved.update(f for f in derived if f not in cited)
                derived_analysts: dict[str, str] = {}
                if unresolved:
                    lu = await conn.fetch(
                        "SELECT id::text AS id, analyst_id FROM analyst_outputs "
                        "WHERE id = ANY($1::uuid[])",
                        sorted(unresolved),
                    )
                    derived_analysts = {
                        lr["id"]: lr["analyst_id"] for lr in lu if lr["analyst_id"]
                    }
            # Run the shared pure reducers per target that has BOTH a scorecard
            # and a composition head; flatten, bound, and stamp each row's target.
            disagreements: list[dict[str, Any]] = []
            for tgt in country_targets:
                dims = dims_by_target.get(tgt)
                comp = comp_by_target.get(tgt)
                if not dims or comp is None:
                    continue
                citations, derived = comp
                for d in scorecard_disagreements(
                    dims, composition_usages(citations, derived, derived_analysts)
                ):
                    item = d.model_dump()
                    item["target_id"] = tgt
                    disagreements.append(item)
            disagreements = disagreements[:_DISAGREEMENT_MAX_ROWS]
            # The contested finding ids ARE returned by this tool now, so the
            # journal may cite them; add them to refs (deduped, order-preserving).
            seen = set(refs)
            for item in disagreements:
                fid = item.get("finding_id")
                if fid and fid not in seen:
                    seen.add(fid)
                    refs.append(fid)
            return disagreements
        except Exception as exc:  # noqa: BLE001 — degrade to honest absence, never break the read
            # WARNING, not DEBUG (B-8): a swallowed reconcile failure used to
            # render as "no disagreements" at DEBUG level, i.e. invisible in
            # production and indistinguishable from a real reconciliation.
            logger.warning(
                "get_assessments.disagreements_unavailable err=%s "
                "(returning null — NOT an empty list; the journal must not "
                "narrate this as agreement)", exc,
            )
            return None

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
        """The platform's CALIBRATION posture — the latest ``calibration_tracking``
        finding, with the SEGREGATED acute-forecast pilot reported HONESTLY
        (plan §5 / §10).

        Reads the freshest calibration finding from ``analyst_outputs``. B0-3
        (read-truth): the writer produces ``kind='finding'`` with
        ``analyst_id='calibration_tracking'`` (deterministic.py OUTPUT_KINDS —
        NO writer emits ``kind='calibration'``), and the metrics live one JSONB
        level down (the row's ``data`` column holds the WHOLE FindingPayload
        dump; the free-form metrics dict is ``data.data`` — the
        eval_country_scorecard precedent). The headline ``brier`` is
        EXOGENOUS-only (the only number that measures calibration against
        reality); the acute-forecast pilot lives in its OWN keys
        (``brier_forecast_acute`` / ``brier_skill_score`` / sample size /
        ready / degenerate / status) and is NEVER pooled into the headline. This
        is the read the journal's deterministic honesty post-step (§10) keys off:
        the forecast leg is UNPROVEN until ``forecast_acute_ready`` AND NOT
        ``forecast_acute_degenerate`` AND ``brier_skill_score > 0``. ``refs``
        carries the finding's row id so the journal can cite it.
        """
        sql = (
            "SELECT id, produced_at, data FROM analyst_outputs "
            "WHERE kind = 'finding' AND analyst_id = 'calibration_tracking' "
            "AND superseded_by IS NULL "
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
        payload = json.loads(raw) if isinstance(raw, str) else (raw or {})
        # One level down: analyst_outputs.data = the FindingPayload dump; the
        # calibration metrics are its free-form `data` dict.
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) else {}
        # Lane E: ONE shared reducer (data/calibration_honesty.py, a stdlib leaf)
        # — every Brier is returned with ITS OWN n, the pooled count is named
        # `pooled_sample_size`, stale populations say so, and the whole read
        # carries the legacy/untrusted label (and is NOT the sealed ledger).
        red = reduce_calibration(data)
        return {
            "available": True,
            "id": str(row["id"]),
            "produced_at": row["produced_at"].isoformat()
                if isinstance(row["produced_at"], datetime) else None,
            **red,
            # Segregated acute-forecast pilot (n < ACUTE_PILOT_MIN_SAMPLE,
            # reported honestly).
            "brier_forecast_acute": data.get("brier_forecast_acute"),
            "brier_forecast_acute_raw": data.get("brier_forecast_acute_raw"),
            "brier_climatology": data.get("brier_climatology"),
            "brier_skill_score": data.get("brier_skill_score"),
            "forecast_acute_sample_size": data.get("forecast_acute_sample_size"),
            "forecast_acute_ready": bool(data.get("forecast_acute_ready")),
            "forecast_acute_degenerate": bool(data.get("forecast_acute_degenerate")),
            "forecast_acute_status": data.get("forecast_acute_status"),
            "refs": [str(row["id"])],
        }

    async def get_run_health(
        self,
        *,
        analyst_id: str | None = None,
        quiet_hours: int = 24,
        limit: int = _MAX_ROW_LIMIT,
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

        HEAD COVERAGE (W2-T6 / M4): the old ``LIMIT 40`` on an
        analyst-ordered DISTINCT ON silently dropped every analyst past the
        40th ALPHABETICALLY — including ``world_assessor``, the
        registry-breakage canary. Now: the default limit covers the whole
        fleet (clamped at ``_MAX_ROW_LIMIT``), the per-analyst heads are
        ordered STALEST-FIRST (a subquery wrap — DISTINCT ON pins its own
        leading ORDER key to analyst_id) so any future clip can only shed
        the freshest/healthiest analysts, and the response states
        ``analysts_scanned`` / ``analysts_total`` / ``truncated`` explicitly.
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        now = datetime.now(timezone.utc)
        analysts_total: int | None = None
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
            # The LAST run per analyst (DISTINCT ON the freshest start),
            # subquery-wrapped so the OUTER order is staleness (oldest last
            # run first) — the quiet/dead analysts always surface before any
            # LIMIT can bite.
            sql = (
                "SELECT run_id, analyst_id, status, cadence_trigger, "
                "       run_started_at, run_ended_at, had_error "
                "FROM ( "
                "  SELECT DISTINCT ON (analyst_id) "
                "         run_id, analyst_id, status, cadence_trigger, "
                "         run_started_at, run_ended_at, "
                "         (error_payload IS NOT NULL) AS had_error "
                "  FROM analyst_traces "
                "  ORDER BY analyst_id, run_started_at DESC "
                ") heads "
                "ORDER BY run_started_at ASC NULLS FIRST LIMIT $1"
            )
            async with self._pool.acquire() as conn:
                records = await conn.fetch(sql, clamped_limit)
                analysts_total = await conn.fetchval(
                    "SELECT count(DISTINCT analyst_id) FROM analyst_traces"
                )
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
        result: dict[str, Any] = {
            "rows": rows,
            "refs": [],
            "count": len(rows),
            "quiet_analysts": sorted(set(quiet)),
            "quiet_threshold_hours": quiet_hours,
        }
        if analyst_id is None:
            # Coverage honesty (W2-T6): say exactly how much of the fleet the
            # rows cover; rows are stalest-first, so a truncated read only
            # ever sheds the freshest analysts.
            total = int(analysts_total or 0)
            result["analysts_scanned"] = len(rows)
            result["analysts_total"] = total
            result["truncated"] = len(rows) < total
        await _consult_health.overlay_run_health(
            self._pool, result, analyst_id=analyst_id)
        return result

    async def get_source_health(
        self,
        *,
        silent_only: bool = False,
        silent_hours: int = 48,
        limit: int = _MAX_ROW_LIMIT,
    ) -> dict[str, Any]:
        """Source-poll HEALTH — which feeds are quiet or erroring (plan §5).

        The body lives in ``substrate_source_health.get_source_health`` (split
        out on 2026-09-28, wave Q, at the size ceiling); this method is the
        port's surface for it and keeps the signature every caller binds.
        """
        return await _source_health.get_source_health(
            self._pool,
            silent_only=silent_only,
            silent_hours=silent_hours,
            limit=limit,
        )

    async def list_due_resolvers(self, *, horizon_days: int = 14,
                                 include_closed: bool = False, limit: int = 50) -> dict[str, Any]:
        """Forecast-ledger questions due within the horizon (W11-F162); body in
        ``substrate_due_resolvers``."""
        return await _due_resolvers.list_due_resolvers(
            self._pool, horizon_days=horizon_days, include_closed=include_closed, limit=limit)

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
        §7.5), PLUS ``recent_entries`` — the last few entries as short-excerpt
        WALK-BACK so it sees its trajectory across windows, not just the single
        prior step. This IS the journal's self-memory: its output is the off-chain
        11th kind (journal_entries), so a substrate ``list_findings(analyst_id=
        journal_*)`` self-read is empty by construction — this method is where its
        own history actually lives. AND a lightweight delta of activity since ``since`` (an
        ISO8601 timestamp — defaults to the prior entry's ``period_end``): counts
        of new findings / situations / nexuses, so the journal sees at a glance
        what the platform metabolized this window. ``refs`` carries the prior
        entry + consolidation ids (the journal's own continuity, which it MAY cite
        — they are journal rows, off-chain, but they are real ids).
        """
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        async with self._pool.acquire() as conn:
            # 2026-08-02 — `valid_until IS NULL` here for the same reason the
            # consolidation read below has always carried it: a SOFT-CLOSED row
            # (migration 0120: tool-JSON envelopes + empty stubs) must leave the
            # journal's own memory, or the repair is cosmetic and the narrator
            # goes on being fed a tool-call envelope as if it were prose.
            prior_entry = await conn.fetchrow(
                "SELECT id, title, body, period_start, period_end, produced_at "
                "FROM journal_entries WHERE entry_kind = 'entry' "
                f"  AND {MEMORY_ROW_FILTER_SQL} "
                "ORDER BY period_end DESC, produced_at DESC LIMIT 1"
            )
            consolidation = await conn.fetchrow(
                "SELECT id, title, body, period_start, period_end, produced_at "
                "FROM journal_entries "
                "WHERE entry_kind = 'consolidation' "
                f"  AND {MEMORY_ROW_FILTER_SQL} "
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
            # ``created_at``, NOT ``updated_at`` — REGISTER-1h (2026-08-29).
            # ``situations`` is a MUTABLE row that ``situation_clustering``
            # UPSERTs every 20 minutes with ``updated_at=NOW()``, so an
            # ``updated_at`` count is a count of the CRON, not of anything that
            # happened. Measured on the live register at the premise review: 44
            # of 89 rows carried an ``updated_at`` inside the last 20 minutes —
            # and the same 44 for the last hour and the last 24 hours — while
            # frames actually CREATED in the last 24 hours numbered ZERO. So the
            # journal desks' "what changed since I last wrote" prompt was told
            # ``new_situations: 44`` for any cursor older than one cadence tick,
            # when the true answer was 0. ``created_at`` is written once at
            # materialization (the upsert's DO UPDATE branch never touches it),
            # so it is the one column on this row that answers "is this NEW".
            # The sibling counters already read a creation-time column
            # (``analyst_outputs.produced_at`` / ``nexuses.produced_at``); this
            # makes the third one consistent with them.
            new_situations = await conn.fetchval(
                "SELECT count(*) FROM situations "
                "WHERE superseded_by IS NULL AND created_at >= $1",
                since_dt,
            )
            new_nexuses = await conn.fetchval(
                "SELECT count(*) FROM nexuses "
                "WHERE valid_until IS NULL AND superseded_by IS NULL "
                "  AND produced_at >= $1",
                since_dt,
            )
            # Self-history WALK-BACK (§7.5 attentional-continuity fix, 2026-07-03).
            # The journal is the 11th, OFF-CHAIN kind — it writes to journal_entries,
            # NOT analyst_outputs — so a ``list_findings(analyst_id=journal_*)``
            # self-read is empty BY CONSTRUCTION and the journal spent every window
            # narrating its own blindness. This is its real memory across windows:
            # the last few entries so it opens a run seeing the road it has actually
            # walked, not just the single prior step. Short excerpts (token control).
            history_rows = await conn.fetch(
                "SELECT id, title, body, period_end, produced_at "
                "FROM journal_entries WHERE entry_kind = 'entry' "
                f"  AND {MEMORY_ROW_FILTER_SQL} "
                "ORDER BY period_end DESC, produced_at DESC LIMIT $1",
                min(clamped_limit, 8),
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

        def _hist_dict(r: Any) -> dict[str, Any]:
            return {
                "id": str(r["id"]),
                "title": r["title"],
                "period_end": r["period_end"].isoformat()
                    if isinstance(r["period_end"], datetime) else None,
                "excerpt": (r["body"] or "")[:600],
            }

        recent_entries = [_hist_dict(r) for r in history_rows]

        refs: list[str] = []
        if prior_entry is not None:
            refs.append(str(prior_entry["id"]))
        if consolidation is not None:
            refs.append(str(consolidation["id"]))
        # The journal MAY cite its own prior entries (real off-chain ids).
        for r in history_rows:
            rid = str(r["id"])
            if rid not in refs:
                refs.append(rid)
        return {
            "prior_entry": _entry_dict(prior_entry),
            "current_consolidation": _entry_dict(consolidation),
            "recent_entries": recent_entries,
            "since": since_dt.isoformat() if isinstance(since_dt, datetime) else None,
            "delta": {
                "new_findings": int(new_findings or 0),
                "new_situations": int(new_situations or 0),
                "new_nexuses": int(new_nexuses or 0),
            },
            "refs": refs,
        }

    async def get_lens_reads(
        self,
        *,
        lens_analyst_ids: list[str],
        since: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """This cycle's VOICES faculty lens reads — the chorus DIFF's material
        (VOICES_BUILD_DESIGN §3.2, LV-1).

        Returns the MOST RECENT ``entry_kind='lens'`` row per ``analyst_id`` in
        ``lens_analyst_ids`` produced since ``since`` (an ISO8601 timestamp;
        defaults self-computed to a 7d window, mirroring get_journal_delta's
        default-to-prior-period pattern — no caller timestamp required). Keeping
        only the newest row per faculty makes the read idempotent under a
        retry/double-fire. ``analyst_ids_seen`` / ``analyst_ids_missing`` let the
        diff NARRATE be honest about an absent faculty rather than silently
        thinning the matrix (§3.4 partial-roster contract). ``refs`` carries the
        lens-row ids — a diff MAY cite a faculty's own read (a real off-chain
        journal id, same as get_journal_delta exposes prior-entry ids).

        ``lens_analyst_ids`` is passed by the CALLER (the journal_read tool), not
        imported here — the kind-module id set must not cross this layer boundary
        into the runtime port."""
        clamped_limit = max(1, min(int(limit), _MAX_ROW_LIMIT))
        # Resolve the window (default: a 7d lookback — the lens tier's weekly beat).
        since_dt: datetime | None = None
        if since:
            try:
                since_dt = datetime.fromisoformat(str(since))
                if since_dt.tzinfo is None:
                    since_dt = since_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                since_dt = None
        if since_dt is None:
            since_dt = datetime.now(timezone.utc) - timedelta(days=7)
        # Defensive: an empty id list means no faculties to gather — return the
        # honest empty roster rather than an unfiltered scan.
        ids = [a for a in (lens_analyst_ids or []) if isinstance(a, str)]
        rows: list[Any] = []
        if ids:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT id, analyst_id, title, body, claims, "
                    "       cited_substrate_refs, period_start, period_end, "
                    "       produced_at, data "
                    "FROM journal_entries "
                    "WHERE entry_kind = 'lens' AND analyst_id = ANY($1::text[]) "
                    # Soft-closed lens rows (mig 0120 empty stubs) stay out of
                    # the chorus the diff reads — same rule as the entry tier.
                    f"  AND {MEMORY_ROW_FILTER_SQL} "
                    "  AND produced_at >= $2 "
                    "ORDER BY produced_at DESC "
                    "LIMIT $3",
                    ids,
                    since_dt,
                    clamped_limit,
                )
        # Keep only the MOST RECENT row per analyst_id (rows arrive produced_at
        # DESC, so the FIRST occurrence per id is the newest — idempotent dedup).
        latest_by_id: dict[str, Any] = {}
        for r in rows:
            aid = r["analyst_id"]
            if aid not in latest_by_id:
                latest_by_id[aid] = r

        def _load_json(raw: Any, default: Any) -> Any:
            # asyncpg hands a jsonb column back as a str (no codec set) — parse it;
            # a dict/list already-parsed passes through. Mirrors the inline pattern
            # used across this port for `data` columns.
            if isinstance(raw, str):
                try:
                    return json.loads(raw)
                except ValueError:
                    return default
            return raw if raw is not None else default

        def _read_dict(r: Any) -> dict[str, Any]:
            return {
                "id": str(r["id"]),
                "analyst_id": r["analyst_id"],
                "title": r["title"],
                "body": (r["body"] or "")[:8000],
                "claims": _load_json(r["claims"], []),
                "cited_substrate_refs": [str(x) for x in (r["cited_substrate_refs"] or [])],
                "period_start": r["period_start"].isoformat()
                    if isinstance(r["period_start"], datetime) else None,
                "period_end": r["period_end"].isoformat()
                    if isinstance(r["period_end"], datetime) else None,
                "produced_at": r["produced_at"].isoformat()
                    if isinstance(r["produced_at"], datetime) else None,
                "data": _load_json(r["data"], {}),
            }

        reads = [_read_dict(latest_by_id[aid]) for aid in ids if aid in latest_by_id]
        seen = [aid for aid in ids if aid in latest_by_id]
        missing = [aid for aid in ids if aid not in latest_by_id]
        refs = [str(latest_by_id[aid]["id"]) for aid in seen]
        return {
            "reads": reads,
            "analyst_ids_seen": seen,
            "analyst_ids_missing": missing,
            "since": since_dt.isoformat() if isinstance(since_dt, datetime) else None,
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
