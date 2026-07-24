# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.analysts.deterministic — L-173 deterministic analyst kind.

Realizes the L-006 B/A/C/D sub-splits (see ``plans/design/legba_analysis_subsplit.md``)
as four sub-handlers under one analyst kind. The kind itself is the
dispatcher; per-run, the bound descriptor's ``options.sub_handler`` selects
which sub-handler runs.

Contract (per ``plans/design/legba_kind_contracts.md`` §5):

  * ``KIND_NAME = "deterministic"`` — registered against the analyst-kind
    namespace at host start.
  * ``async def run_method(inputs, options, deps) -> AnalystMethodResult`` —
    the entry point the runtime calls per analyst-actor run. The runtime
    in :mod:`legba.runtime.dapr_actors` invokes the bound ``run_method`` as
    ``await deps_bundle.run_method(inputs, options)``; the host wraps this
    function with the activation-time ``deps`` (a ``StandardDeps`` bundle)
    via ``functools.partial`` so the wire-level signature stays 2-arg while
    the kind-internal signature carries deps explicitly.

Differentiating this from LLM-bearing kinds (``inline_target``,
``cross_target_raw``, ``meta_findings_synthesizer``, etc.):

  * **No LLM calls.** All work is pure Python over already-materialized
    substrate slices: networkx for graph mining, scipy/numpy for anomaly
    stats. Token usage is always zeroed.
  * **Structured outputs.** Each sub-handler emits a typed payload whose
    ``data`` field carries the structured result (community ids, anomaly
    scores, Brier values, etc.). ``body`` is a short human-readable
    summary, never a model-generated narrative.

Sub-handlers
------------

================== ==================================================
``graph_mining``    Community detection, structural-balance triads
                    (passthrough — see ``structural_balance`` for the
                    standalone variant), proxy-chain mining over the
                    Apache AGE knowledge graph (``legba_graph``).
``anomaly_detection`` Signal-volume rate-spikes, sentiment-shift
                    z-scores, novel-entity emergence over recent
                    ``time_bucket()`` windows on the primary Postgres pool.
``structural_balance`` Signed-edge triadic balance on the entity-
                    relationship graph (AlliedWith / HostileTo /
                    HostileTo via reified-Nexus intent).
``calibration_tracking`` Analyst confidence-vs-outcome tracking —
                    Brier score, rolling reliability bins, drift
                    detection across windows.
================== ==================================================

Each sub-handler is a separate module under ``deterministic_handlers/`` so
the optimizer (L-176) can iterate on one without disturbing the others, and
so descriptors that need only one sub-handler can import the symbol
directly without paying the others' import cost.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ..provenance.kinds import TRACE_ONLY, OutputKind
from ..provenance.models import FindingPayload
from ...runtime.analyst_method import AnalystMethodResult
from .deterministic_handlers import (
    adversarial_signals,
    anomaly_detection,
    calibration_tracking,
    collection_gap,
    composition_lineage_sweep,
    corpus_indexer,
    cross_source_coalesce,
    cross_source_dedup,
    entity_gc,
    entity_resolution,
    fact_contention_arbiter,
    fact_decay,
    finding_supersession,
    forecast_scoreboard,
    hypothesis_lifecycle,
    indicator_tracker,
    situation_clustering,
    thematic_proposal,
    graph_mining,
    integrity_sweep,
    nexus_decay,
    proposed_edge_governance,
    reenrich_ner,
    reenrich_translation,
    scorecard_producer,
    signal_embedder,
    signal_summarizer,
    signals_retention,
    structural_balance,
    unit_correctness_scorer,
)

logger = logging.getLogger(__name__)

KIND_NAME: str = "deterministic"

# Default OUTPUT_KIND for deterministic — the sub-handlers that produce a
# genuine analytical FINDING (graph_mining / anomaly_detection /
# situation_clustering / …) write a FindingPayload-shaped row.  The "Findings
# as a real output type" cleanup splits the table: maintenance sub-handlers
# whose REAL product is side-written (nexuses / decayed-fact stamps / dedup
# links / governance promotions) are marked ``TRACE_ONLY`` so they STOP
# emitting a redundant FINDING receipt into ``analyst_outputs`` — the run is
# already fully audited in ``analyst_traces`` (run summary in
# ``analyst_traces.output_payload``).  The dispatcher (the actor's output-
# dispatch chokepoint in :mod:`legba.runtime.dapr_actors`) resolves the
# effective kind per-run via ``OUTPUT_KIND_BY_SUB_HANDLER.get(sub_handler,
# OUTPUT_KIND)``; a ``TRACE_ONLY`` value skips the analyst_outputs INSERT while
# keeping the trace + the in-run_method side-writes.
OUTPUT_KIND: OutputKind = OutputKind.FINDING
OUTPUT_KIND_BY_SUB_HANDLER: dict[str, object] = {
    # --- KEEP emitting a real FINDING (substantive analytical product) ---
    # L-173 graph mining — communities / proxy-chains. A genuine finding.
    "graph_mining": OutputKind.FINDING,
    # Volume/sentiment/novel-entity anomaly scores. A genuine finding.
    "anomaly_detection": OutputKind.FINDING,
    # Confidence-vs-outcome Brier/reliability tracking. A genuine finding.
    "calibration_tracking": OutputKind.FINDING,
    # P2-T5 per-unit correctness-vs-reference (source-id overlap recall vs the
    # gold labels) + faithfulness mean. A genuine measurement finding.
    "unit_correctness_scorer": OutputKind.FINDING,
    # P3-T6 composition lineage-integrity sweep — the crown's per-floor
    # derived_from BFS over the composition roots (0 dangling/cycles on a healthy
    # tower; NAMES a broken sub-claim floor). A substantive verification product.
    "composition_lineage_sweep": OutputKind.FINDING,
    # Adversarial-signal scoring. A genuine finding.
    "adversarial_signals": OutputKind.FINDING,
    # Situation clustering — materializes the `situations` table from the
    # signatures supersession stamps; the run summary is a FindingPayload (the
    # situation rows are side-written directly to the situations table).
    # KEEP per the operator-confirmed split (verified: situation_clustering's
    # OUTPUT_KIND stays FINDING — its summary IS a substantive product).
    "situation_clustering": OutputKind.FINDING,
    # thematic_proposal (5b) — PROPOSES thematic frames for uncovered hot
    # situations. The proposal finding is a user-facing, actionable product (an
    # operator reads it + registers the suggested target), so it is a real
    # FINDING, NOT trace-only.
    "thematic_proposal": OutputKind.FINDING,
    # S3-T2 indicator_tracker — diffs the structured I&W indicators run-over-run
    # per unit-stream + emits a summary FINDING on status FLIPS (esp.
    # not_observed→triggered). A user-facing analytical product (the fired
    # warning signposts a duty officer reads); a no-flip sweep is suppressed via
    # the run's force_trace_only (NOT this map — the map is the always-on kind).
    "indicator_tracker": OutputKind.FINDING,
    # S3-T3 collection_gap — monthly aggregation of the scorecard
    # insufficient-evidence signal into a "collection requirements" FINDING
    # (which desk×dimension cells are starved + the source classes that would
    # feed them). A user-facing collection-management product; a no-gap sweep is
    # suppressed via the run's force_trace_only (NOT this map).
    "collection_gap": OutputKind.FINDING,
    # Hypothesis lifecycle (Piece 3, Task D) — side-writes HYPOTHESIS rows via
    # write_hypothesis + returns a FindingPayload summary. NOT in the
    # operator-confirmed trace-only list, so unchanged here (left FINDING).
    "hypothesis_lifecycle": OutputKind.FINDING,
    # Signals TTL purge — NOT in the operator-confirmed trace-only list;
    # disabled by default (ttl_days<=0). Left FINDING (unchanged).
    "signals_retention": OutputKind.FINDING,

    # --- TRACE-ONLY (real product is side-written; run audited in the trace;
    #     stop the redundant analyst_outputs FINDING receipt) ---
    # Signed-edge triadic balance over the reified-Nexus graph — its result is
    # a structural-balance summary already captured in the trace.
    "structural_balance": TRACE_ONLY,
    # L-203 migrated maintenance modules — pure substrate maintenance, no
    # analytical finding: GC of orphaned entities, canonical entity merges,
    # temporal fact-decay stamps, nexus-decay stamps.
    "entity_gc": TRACE_ONLY,
    "entity_resolution": TRACE_ONLY,
    "fact_decay": TRACE_ONLY,
    # Holes-B contested-claims arbiter (#101) — DETECT-ONLY. Its real product is
    # side-written (the fact_contention* sidecar + the facts marker columns); the
    # per-run counts (groups open / abstained / junk-excluded) live in the trace.
    "fact_contention_arbiter": TRACE_ONLY,
    # signal_summarizer — pure side-effect sweep: writes OUR analysis-tuned
    # summary into signals.payload.distilled_body + stamps summarized_at. Emits
    # no analytical finding (like entity_resolution); the per-run counts live in
    # the trace.
    "signal_summarizer": TRACE_ONLY,
    # corpus_indexer — pure side-effect sweep: projects signals into the
    # OpenSearch full-text corpus (the INDEX PLANE) + stamps indexed_at. Emits no
    # analytical finding (like signal_summarizer / entity_resolution); the per-run
    # counts (examined / indexed / failed) live in the trace.
    "corpus_indexer": TRACE_ONLY,
    # signal_embedder — pure side-effect sweep: projects signals into the Qdrant
    # legba_signals collection (the VECTOR PLANE — semantic retrieval that lights
    # up vector_search) + stamps embedding_ref. Emits no analytical finding (like
    # corpus_indexer / signal_summarizer); the per-run counts (examined / embedded
    # / failed) live in the trace.
    "signal_embedder": TRACE_ONLY,
    # reenrich_ner — ONE-TIME NER backfill sweep: re-runs the LIVE multilingual/
    # telegram NER over the pre-fix historical backlog, writing payload.entities +
    # promoting entity_classes + resetting entities_resolved_at (so entity_resolution
    # re-folds them) + stamping reenriched_at. Its real product is the side-written
    # signal enrichment (like signal_summarizer / entity_resolution); the per-run
    # counts (examined / reenriched / failed) live in the trace.
    "reenrich_ner": TRACE_ONLY,
    # reenrich_translation — TRANSLATION backfill sweep (M13/T-1c): translates the
    # non-Latin backlog's title (+ body when present) via the hosted /translate,
    # side-writing payload.title_en / payload.text_en so readers narrate English,
    # not a transliterated surface. Its real product is the side-written signal
    # enrichment (like reenrich_ner); the per-run counts live in the trace.
    "reenrich_translation": TRACE_ONLY,
    "nexus_decay": TRACE_ONLY,
    # P-09 cross-source dedup (PIVOT §4.3 / P-02) — links/marks duplicate
    # signals; the dedup counts live in the trace.
    "cross_source_dedup": TRACE_ONLY,
    # P2 cross-source semantic/temporal coalesce — substrate-wide near-dup
    # linker; the coalesce counts live in the trace.
    "cross_source_coalesce": TRACE_ONLY,
    # P-FS finding-level dedup / supersession — stamps supersession on existing
    # findings; the superseded counts live in the trace.
    "finding_supersession": TRACE_ONLY,
    # Re-homed referential-integrity sweep — repairs/flags dangling refs; the
    # sweep counts live in the trace.
    "integrity_sweep": TRACE_ONLY,
    # Proposed-edge governance (FIX P3-1) — promotes corroborated co_occurs
    # proposed_edges into nexuses (via the live write_nexus side-write) + ages
    # out thin stale ones; the promotion/aging counts live in the trace.
    "proposed_edge_governance": TRACE_ONLY,
    # P4-T2 banded-scorecard producer — the REAL product = the N side-written
    # `kind=scorecard` rows (one banded verdict per active G20 country); the
    # returned summary is a per-run RECEIPT fully audited in analyst_traces, so it
    # belongs in the TRACE-ONLY bucket alongside structural_balance /
    # proposed_edge_governance. NOTE the split is intentional: TRACE_ONLY governs
    # ONLY the single returned summary finding — the per-country side-writes pick
    # their OWN kind=scorecard inside write_analyst_output and are UNAFFECTED by
    # this map (they are genuine persisted rows, not trace-only).
    "scorecard_producer": TRACE_ONLY,
    # P4-T7 acute-forecast scoreboard producer — the REAL product = the
    # side-written `acute_forecasts` rows (issued / exogenously resolved by the
    # forecast_acute writers this handler DRIVES); the returned summary is a
    # per-run counts RECEIPT fully audited in analyst_traces. TRACE_ONLY so the
    # receipt NEVER lands a finding / prediction / claim on any trust surface —
    # forecasting surfaces ONLY as acute_forecasts rows + the T4 scoreboard.
    "forecast_scoreboard": TRACE_ONLY,
}

# READ_SLICE defaults to the signals reader — graph_mining + anomaly +
# structural_balance + calibration_tracking all reason over signals.
READ_SLICE = None

# Sub-handler dispatch table. Keys must match the descriptor's
# ``options.sub_handler`` field exactly. Add new sub-handlers by appending
# here + dropping the module in ``deterministic_handlers/``.
SUB_HANDLERS: dict[str, Any] = {
    # L-173 original sub-handlers
    "graph_mining": graph_mining.handle,
    "anomaly_detection": anomaly_detection.handle,
    "structural_balance": structural_balance.handle,
    "calibration_tracking": calibration_tracking.handle,
    # P2-T5 per-unit correctness-vs-reference scorer (deterministic, LLM-free) —
    # source-id overlap RECALL of each bounded unit's latest head finding vs the
    # operator-authored gold rows; honest-None when nothing is scorable.
    "unit_correctness_scorer": unit_correctness_scorer.handle,
    # P3-T6 composition lineage-integrity sweep — per-floor derived_from BFS
    # (validate_lineage) over the world/country composition roots. Refuses loud.
    "composition_lineage_sweep": composition_lineage_sweep.handle,
    # L-203 migrated maintenance modules
    "adversarial_signals": adversarial_signals.handle,
    "entity_gc": entity_gc.handle,
    "entity_resolution": entity_resolution.handle,
    "fact_decay": fact_decay.handle,
    # Holes-B contested-claims arbiter (#101, DETECT-ONLY) — builds the
    # fact_contention* sidecar + stamps the facts markers; never closes a fact.
    "fact_contention_arbiter": fact_contention_arbiter.handle,
    # signal_summarizer — async sweep that distills long signal bodies into
    # payload.distilled_body via the CORE self-hosted LLM plane ($0), so
    # downstream synthesis reads OUR analysis-tuned brief instead of the
    # publisher's teaser. Idempotent + forward-progressing (stamps
    # signals.summarized_at on every examined row).
    "signal_summarizer": signal_summarizer.handle,
    # corpus_indexer — async sweep that projects signals into the OpenSearch
    # full-text corpus (the INDEX PLANE — BM25 lexical mining substrate over the
    # shared pool). Idempotent + forward-progressing (stamps signals.indexed_at;
    # OpenSearch `_id` = the signal id, so a re-index overwrites in place).
    "corpus_indexer": corpus_indexer.handle,
    # signal_embedder — async sweep that embeds signal bodies into the Qdrant
    # legba_signals collection (the VECTOR PLANE — semantic retrieval that lights
    # up vector_search, which no-ops today with 0 points). Idempotent +
    # forward-progressing (stamps signals.embedding_ref; the Qdrant point _id =
    # the signal id, so a re-embed overwrites in place).
    "signal_embedder": signal_embedder.handle,
    # reenrich_ner — ONE-TIME async backfill sweep that re-runs the LIVE
    # NERMultilingualHandler (translate-then-NER + telegram payload.text) over the
    # ~9,143 signals ingested BEFORE the multilingual/telegram fix landed (they carry
    # 0 entities). Reuses the production handler (never reimplements NER); idempotent
    # + forward-progressing (stamps signals.reenriched_at on every examined row).
    "reenrich_ner": reenrich_ner.handle,
    # reenrich_translation — TRANSLATION-backfill sweep (M13/T-1c) that translates
    # the ~1.9k non-Latin signals ingested BEFORE T-1a stamped title_en/text_en at
    # ingest. Reuses the hosted /translate plane (never reimplements it); idempotent
    # + forward-progressing (the field IS the marker — title_en NULL is the gate).
    "reenrich_translation": reenrich_translation.handle,
    "nexus_decay": nexus_decay.handle,
    # P-09 cross-source dedup (PIVOT §4.3 / P-02)
    "cross_source_dedup": cross_source_dedup.handle,
    # P2 cross-source semantic/temporal coalesce (review data-integrity) — the
    # substrate-wide near-dup linker (reuses Dedupe4TierHandler tier-3/4).
    "cross_source_coalesce": cross_source_coalesce.handle,
    # P-FS finding-level dedup / supersession (PIVOT_BUILD_PLAN §12, W3)
    "finding_supersession": finding_supersession.handle,
    # Situation clustering — materializes `situations` from stamped signatures.
    "situation_clustering": situation_clustering.handle,
    # Thematic proposal (5b) — proposes thematic frames for uncovered hot
    # situations (detect → propose → operator-promote).
    "thematic_proposal": thematic_proposal.handle,
    # S3-T2 indicator_tracker — deterministic run-over-run diff of the structured
    # I&W indicators per (target_id, source unit analyst_id); emits a summary
    # finding on status flips, trace-only on a no-flip/unchanged sweep.
    "indicator_tracker": indicator_tracker.handle,
    # S3-T3 collection_gap — monthly deterministic aggregation of the scorecard
    # insufficient-evidence signal per desk×dimension into a "collection
    # requirements" finding (starved cells + the plausible feed source classes);
    # trace-only when nothing is starved.
    "collection_gap": collection_gap.handle,
    # Hypothesis lifecycle (Piece 3, Task D) — emits forward-claim hypotheses
    # over rising situations + tests standing ones vs later evidence. Side-writes
    # HYPOTHESIS rows via the live write_hypothesis path; returns a FINDING summary.
    "hypothesis_lifecycle": hypothesis_lifecycle.handle,
    # Re-homed referential-integrity sweep (DIRECTION §9 — events-free successor
    # to the 2.4-deleted integrity_verification)
    "integrity_sweep": integrity_sweep.handle,
    # Signals TTL purge (graph-and-data Wave-1b item 3 / D4). Disabled by
    # default (ttl_days<=0); operator opts in with a positive TTL on options.
    "signals_retention": signals_retention.handle,
    # Proposed-edge governance (FIX P3-1) — promotes corroborated co_occurs
    # proposed_edges to nexuses + flips status; ages out thin stale ones.
    "proposed_edge_governance": proposed_edge_governance.handle,
    # P4-T2 banded-scorecard producer — global sweep over active G20 countries;
    # side-writes one kind=scorecard row per country (data.bands = the T1 verdict,
    # T5 eval folded), returns a TRACE_ONLY summary receipt.
    "scorecard_producer": scorecard_producer.handle,
    # P4-T7 acute-forecast scoreboard producer — weekly-idempotent driver for the
    # forecast_acute pilot (issue → exogenous-resolve → count). Side-writes the
    # acute_forecasts rows via the existing forecast_acute writers; returns a
    # TRACE_ONLY counts receipt. Forecasting surfaces only in the T4 scoreboard.
    "forecast_scoreboard": forecast_scoreboard.handle,
}


class DeterministicDispatchError(ValueError):
    """Raised when ``options.sub_handler`` is missing or unknown."""


def _resolve_sub_handler_name(options: Mapping[str, Any]) -> str:
    """Pick the sub-handler name out of ``options``.

    Accepts the top-level ``sub_handler`` key. Raises
    :class:`DeterministicDispatchError` if missing or unknown so the
    runtime can route the failure to the trace + output DLQ rather than
    silently no-op.
    """
    name = options.get("sub_handler")
    if not name:
        raise DeterministicDispatchError(
            "deterministic kind requires options['sub_handler'] "
            f"(one of {sorted(SUB_HANDLERS)!r})"
        )
    if name not in SUB_HANDLERS:
        raise DeterministicDispatchError(
            f"unknown deterministic sub_handler {name!r}; "
            f"valid: {sorted(SUB_HANDLERS)!r}"
        )
    return name


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None = None,
) -> AnalystMethodResult:
    """Top-level deterministic-kind dispatcher.

    Parameters
    ----------
    inputs:
        Already-materialized substrate rows the runtime fetched (per the
        descriptor's subscription). Each sub-handler treats these as a
        seed — graph mining may pull additional edges over Apache AGE,
        anomaly detection may pull historical buckets off the primary
        Postgres pool, etc. — but the row list bounds the run.
    options:
        At minimum ``{"sub_handler": "<name>", "analyst_id": ..., "run_id":
        ...}``. Sub-handlers may consume additional keys; see their docs.
    deps:
        :class:`legba.runtime.deps.StandardDeps` (or any object exposing
        ``pg_pool`` etc. for the sub-handler that needs it). ``None`` is
        accepted for unit-test paths that pre-shape inputs and don't
        require live substrate access.

    Returns
    -------
    :class:`AnalystMethodResult`
        ``finding`` is a :class:`FindingPayload` with structured ``data``
        carrying the sub-handler's results. ``usage`` is always
        ``{"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens":
        0}`` because no LLM is invoked.

    Raises
    ------
    DeterministicDispatchError
        Missing or unknown ``options['sub_handler']``.
    """
    sub_handler_name = _resolve_sub_handler_name(options)
    handler = SUB_HANDLERS[sub_handler_name]
    logger.info(
        "analysts.deterministic.dispatch sub_handler=%s analyst_id=%s run_id=%s",
        sub_handler_name,
        options.get("analyst_id"),
        options.get("run_id"),
    )
    result = await handler(inputs, options, deps)
    if not isinstance(result, AnalystMethodResult):
        raise TypeError(
            f"deterministic sub_handler {sub_handler_name!r} returned "
            f"{type(result).__name__}, expected AnalystMethodResult"
        )
    return result


__all__ = [
    "AnalystMethodResult",
    "DeterministicDispatchError",
    "FindingPayload",
    "KIND_NAME",
    "OUTPUT_KIND",
    "OUTPUT_KIND_BY_SUB_HANDLER",
    "READ_SLICE",
    "SUB_HANDLERS",
    "TRACE_ONLY",
    "run_method",
]
