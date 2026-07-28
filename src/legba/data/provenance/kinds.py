# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Output-kind registry — per topology §4.5.

Each registered analyst-output kind declares:

  * `kind` — the canonical kind name (string id, also the enum value).
  * `table` — the substrate table that receives the row.
  * `payload_model` — pydantic model the analyst output must validate against.
  * `schema_uri` — Iglu URI (per L-090 §4.6) embedded in the row's
    `schema_uri` column at write time. The default points at the current
    family/major-minor-patch; analyst descriptors can override per L-101 §7.
  * `nats_subject_pattern` — NATS subject the write helper publishes on after
    insert. `{analyst_id}` and `{target_id}` placeholders are substituted at
    publish time. None means "no event."

Registry is plain dict + ``register_kind`` so downstream Phase 6 analyst kinds
can add custom output kinds without modifying this file (open taxonomy per
L-101 §8 vocabulary).

Table-routing decisions for Phase 1:

  * ``situation``  → ``situations`` (dedicated table).
  * ``hypothesis`` → ``hypotheses`` (dedicated table).
  * ``journal``    → ``journal_entries`` (dedicated table, migration 0048).
    The 11th OutputKind — the first-person reflective voice. OFF the
    fact/finding/nexus chain (NOT a fact source): a journal row is a
    *perspective over* the provenance chain, never a *member of* it. It carries
    an ALWAYS-EMPTY ``derived_from`` and is deliberately absent from the lineage
    catalog so a downstream lineage walk can never surface it (plan §3.5).
  * ``prediction`` → ``analyst_outputs`` (source-first pivot, migration 0024
    DROPPED the dedicated ``predictions`` table; predictions now land as a
    normal generic-table row with ``kind='prediction'``).
  * ``finding`` / ``meta_finding`` / ``alert`` / ``critique`` →
    ``analyst_outputs`` (new generic table; see migration 0011).

When Phase 8 (L-190) introduces dedicated tables for ``finding`` etc.,
update the registry mapping — call sites remain unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Type

from pydantic import BaseModel

from .models import (
    AlertPayload,
    CritiquePayload,
    FactPayload,
    FindingPayload,
    HypothesisPayload,
    JournalPayload,
    MetaFindingPayload,
    NexusPayload,
    PredictionPayload,
    PromptModuleCandidatePayload,
    ScorecardPayload,
    SituationPayload,
)


class OutputKind(str, Enum):
    """Canonical analyst-output kinds (topology §4.5).

    NOTE: there is deliberately NO ``signal`` kind here.  Signals are not
    analyst outputs — they are source-owned rows written by the canonical
    ingestion path (``legba.runtime.source_actor.write_canonical_signal``,
    source-first pivot / migration 0024), which stamps its own
    ``schema_uri`` and never routes through this registry.  The stale
    pre-pivot ``SIGNAL`` entry (it targeted the dropped target-owned
    ``signals`` shape) was removed by C-3.
    """

    FINDING = "finding"
    SITUATION = "situation"
    HYPOTHESIS = "hypothesis"
    PREDICTION = "prediction"
    ALERT = "alert"
    META_FINDING = "meta_finding"
    CRITIQUE = "critique"
    # Altitude-0 extraction (anchor §5 PIECE 2). Lands in the dedicated
    # `facts` table. Produced both by the ingest-time `fact_extractor`
    # enrichment stage (source-owned) and by analyst/workflow `write_fact`.
    FACT = "fact"
    # PIECE A — reified typed relationship.  Lands in the dedicated `nexuses`
    # table (migration 0033).  Produced by the `relationship_reifier` META
    # analyst kind (8B-LLM typed: label + canonical polarity sign + intent),
    # written via `write_nexus` with temporal bounds + supersession.
    NEXUS = "nexus"
    # L-176 optimizer candidate prompt module.  Lands in the generic
    # `analyst_outputs` table; promotion to live is gated downstream.
    PROMPT_MODULE_CANDIDATE = "prompt_module_candidate"
    # The 11th kind — Legba's first-person reflective voice (plan §3.2).
    # Lands in the dedicated `journal_entries` table (migration 0048). Produced
    # by the `journal_assessor` META analyst kind. OFF the fact/finding/nexus
    # chain: it must NEVER write a fact/finding/nexus (§3.1). Direction-
    # asymmetric lineage node — empty `derived_from`, excluded from the
    # downstream lineage fan-out (§3.5).
    JOURNAL = "journal"
    # The 12th kind — P4-T2 banded per-country verdict (the HONEST top of the
    # the system). Lands in the generic `analyst_outputs` table, one row per
    # active G20 country. A *perspective over* already-verified sub-claims: its
    # `derived_from` NAMES the basis findings the bands rest on (a P1 lineage walk
    # resolves them), and NO band ever exists without a real basis id — an
    # insufficient-evidence dimension carries an empty-but-explicit basis.
    SCORECARD = "scorecard"


class _TraceOnly:
    """Sentinel "output kind" for META analysts that are fully audited in
    ``analyst_traces`` and whose REAL product is side-written.

    A kind (or deterministic sub-handler) declaring ``TRACE_ONLY`` instead of
    a real :class:`OutputKind` tells the actor's output-dispatch chokepoint to
    SKIP the ``analyst_outputs`` row while still:

      * running the kind's in-``run_method`` side-writes
        (``write_nexus`` / ``write_hypothesis`` / ``write_graph_metric`` / …);
      * writing the ``analyst_traces`` receipt row (the run summary survives in
        ``analyst_traces.output_payload`` — nothing is lost).

    This is what makes ``FINDING`` a genuine OutputKind: the META kinds
    (relationship_reifier, competing_hypotheses, the deterministic maintenance
    sub-handlers) stop emitting redundant FINDING *receipts* whose only purpose
    was to record that a run happened — every run is already in the trace.

    It is a singleton (``TRACE_ONLY``) with a stable ``repr`` so it reads
    cleanly in logs and dispatch tables. Identity comparison (``is``) is the
    contract; do NOT treat it as an :class:`OutputKind`.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "TRACE_ONLY"


# The single shared sentinel instance. Compare with ``is TRACE_ONLY``.
TRACE_ONLY = _TraceOnly()

# ---------------------------------------------------------------------------
# P0-4 — verify-EXEMPT structural analysts (the honest badge registry)
# ---------------------------------------------------------------------------
# The mandatory faithfulness verify pass (actor_critic.verify_inline_target_
# finding) fires ONLY for inline_target findings, the verify-declaring
# meta_findings_synthesizer / cross_analyst_correlator compositions, and the
# journal profile. The DETERMINISTIC sub-handlers that emit a genuine FINDING
# (deterministic.OUTPUT_KIND_BY_SUB_HANDLER → OutputKind.FINDING) never enter
# that pass — they are pure structural/mining reads (no LLM prose to grade)
# carrying flat confidence, and in feed contexts their rows were visually
# indistinguishable from verified ones. This registry NAMES that exception so
# every read surface can render an explicit ``unverified — structural`` badge
# instead of a quiet nothing.
#
# By convention each deterministic descriptor's identity.id == its
# options.sub_handler, so these double as analyst_ids. The set MUST stay equal
# to the FINDING-emitting sub-handlers — the drift guard in
# tests/data_pkg/test_trace_only_output_split.py asserts equality. Mirror:
# legba-ui-v3/src/lib/verdictModel.ts STRUCTURAL_VERIFY_EXEMPT_ANALYSTS (the
# live-tail rows never pass through the reads-API stamp).
STRUCTURAL_VERIFY_EXEMPT_ANALYSTS: frozenset[str] = frozenset({
    "graph_mining",
    "anomaly_detection",
    "band_calibration_tracker",
    "calibration_tracking",
    "unit_correctness_scorer",
    "composition_lineage_sweep",
    "adversarial_signals",
    "situation_clustering",
    "thematic_proposal",
    "indicator_tracker",
    "collection_gap",
    "hypothesis_lifecycle",
    "signals_retention",
    "analyst_traces_retention",
    "geo_convergence_scan",
    "fact_decay_scan",
    "source_track_record",
    "narrative_mapper",
    "desk_baseline",
})


def verify_exempt_reason(analyst_id: str | None) -> str | None:
    """The verify-exemption tag for an analyst's findings, or ``None``.

    ``"structural"`` when ``analyst_id`` is a deterministic structural/mining
    analyst whose findings never route through the faithfulness verify pass —
    the reads API stamps this onto every projected finding row so no client
    has to guess. ``None`` for every verified (or unknown) analyst: the badge
    is never fabricated for a row we cannot classify.
    """
    if analyst_id is not None and analyst_id in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS:
        return "structural"
    return None


# ---------------------------------------------------------------------------
# C2b (P4-6) — structural_claims verify OPT-IN registry (the honest badge, made
# real for CLAIM-BEARING structural findings)
# ---------------------------------------------------------------------------
# A SUBSET of STRUCTURAL_VERIFY_EXEMPT_ANALYSTS: the structural analysts whose
# findings assert a CHECKABLE QUANTITY (a distinct-count over a converged cell,
# an echo count over a carrier set, an arithmetic rollup identity) and therefore
# get a REAL deterministic re-derivation verify (verify.verify_structural_claims)
# instead of only the ``unverified — structural`` badge. Pure-telemetry members
# of the exempt set (retention scans, honest-summary-only handlers) stay OUT —
# their findings are non-verifiable aggregates and keep the plain badge.
#
# ONE declared place (mirrors the STRUCTURAL_VERIFY_EXEMPT_ANALYSTS precedent),
# NOT scattered per call-site. A drift guard
# (tests/data_pkg/test_structural_claims_verify.py) asserts this stays a SUBSET
# of the exempt set — you cannot structurally-verify a non-structural analyst.
# An opted-in analyst whose finding carries no ``data['structural_claims']``
# block is a NO-OP (no critique written; the row keeps its honest structural
# badge), so listing an analyst here before it emits the block is harmless.
STRUCTURAL_CLAIMS_VERIFY_ANALYSTS: frozenset[str] = frozenset({
    "geo_convergence_scan",
    "indicator_tracker",
    "thematic_proposal",
    # narrative_mapper landed in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS (P4-1 wave)
    # and emits a re-derivable rollup identity (narratives_total = contested +
    # surfaced, always true by REIFIED_STATUSES construction) — so it joins the
    # claims-verified set per the C2b merge note. Subset drift guard holds.
    "narrative_mapper",
})


def structural_claims_verify_opt_in(analyst_id: str | None) -> bool:
    """Whether ``analyst_id`` opts into the deterministic structural_claims
    verify profile (C2b). False for every non-opted-in analyst."""
    return analyst_id is not None and analyst_id in STRUCTURAL_CLAIMS_VERIFY_ANALYSTS


def structural_badge(analyst_id: str | None, structural_verified: bool | None) -> str | None:
    """The ``verify_exempt`` badge stamp, folding a structural verdict (C2b).

    Extends :func:`verify_exempt_reason`: a structural finding that now carries a
    PASSING structural critique (``structural_verified is True``) reads
    ``"structural-verified"``; one without (or a failed / unverifiable verdict)
    keeps the honest ``"structural"`` (rendered ``unverified — structural``).
    ``None`` for every non-structural analyst — never fabricated.
    """
    base = verify_exempt_reason(analyst_id)
    if base == "structural" and structural_verified is True:
        return "structural-verified"
    return base

# An "effective output kind" is either a real OutputKind (writes a row) or the
# TRACE_ONLY sentinel (skip the row, keep the trace + side-writes).
EffectiveOutputKind = "OutputKind | _TraceOnly"


@dataclass(frozen=True)
class OutputKindSpec:
    kind: OutputKind
    table: str
    payload_model: Type[BaseModel]
    schema_uri: str
    nats_subject_pattern: str | None


# Iglu URIs per L-090 §4.6 — initial set.
_FINDING_URI       = "iglu:legba/finding/jsonschema/1-0-0"
_SITUATION_URI     = "iglu:legba/situation/jsonschema/2-0-0"
_HYPOTHESIS_URI    = "iglu:legba/hypothesis/jsonschema/2-0-0"
_PREDICTION_URI    = "iglu:legba/prediction/jsonschema/2-0-0"
_ALERT_URI         = "iglu:legba/alert/jsonschema/1-0-0"
_META_FINDING_URI  = "iglu:legba/meta_finding/jsonschema/1-0-0"
_CRITIQUE_URI      = "iglu:legba/critique/jsonschema/1-0-0"
# Matches the DB default on `facts.schema_uri` (0001_baseline.sql:499).
_FACT_URI          = "iglu:legba/fact/jsonschema/2-0-0"
# Matches the DB default on `nexuses.schema_uri` (0033_nexuses.sql).
_NEXUS_URI         = "iglu:legba/nexus/jsonschema/1-0-0"
_PROMPT_MODULE_CANDIDATE_URI = (
    "iglu:legba/prompt_module_candidate/jsonschema/1-0-0"
)
# Matches the DB default on `journal_entries.schema_uri` (0048_journal.sql).
_JOURNAL_URI       = "iglu:legba/journal/jsonschema/1-0-0"
# P4-T2 banded per-country verdict — lands in the generic `analyst_outputs`
# table (no dedicated table / DB default), so the URI is declared here only.
_SCORECARD_URI     = "iglu:legba/scorecard/jsonschema/1-0-0"


KIND_REGISTRY: dict[OutputKind, OutputKindSpec] = {
    OutputKind.FINDING: OutputKindSpec(
        kind=OutputKind.FINDING,
        table="analyst_outputs",
        payload_model=FindingPayload,
        schema_uri=_FINDING_URI,
        nats_subject_pattern="analyst.{analyst_id}.finding",
    ),
    OutputKind.SITUATION: OutputKindSpec(
        kind=OutputKind.SITUATION,
        table="situations",
        payload_model=SituationPayload,
        schema_uri=_SITUATION_URI,
        nats_subject_pattern="analyst.{analyst_id}.situation",
    ),
    OutputKind.HYPOTHESIS: OutputKindSpec(
        kind=OutputKind.HYPOTHESIS,
        table="hypotheses",
        payload_model=HypothesisPayload,
        schema_uri=_HYPOTHESIS_URI,
        nats_subject_pattern="analyst.{analyst_id}.hypothesis",
    ),
    OutputKind.PREDICTION: OutputKindSpec(
        kind=OutputKind.PREDICTION,
        # Source-first pivot (migration 0024) DROPPED the `predictions` table.
        # Predictions now persist as a normal `analyst_outputs` row
        # (kind=prediction); the numerics also live in
        # `analyst_traces.output_payload`. The C-1 stopgap `/predictions`
        # read route was retired (no live predictor analyst); predictions
        # surface via the generic analyst-outputs reads. Do NOT recreate
        # the `predictions` table.
        table="analyst_outputs",
        payload_model=PredictionPayload,
        schema_uri=_PREDICTION_URI,
        nats_subject_pattern="analyst.{analyst_id}.prediction",
    ),
    OutputKind.ALERT: OutputKindSpec(
        kind=OutputKind.ALERT,
        table="analyst_outputs",
        payload_model=AlertPayload,
        schema_uri=_ALERT_URI,
        nats_subject_pattern="alerts.{analyst_id}",
    ),
    OutputKind.META_FINDING: OutputKindSpec(
        kind=OutputKind.META_FINDING,
        table="analyst_outputs",
        payload_model=MetaFindingPayload,
        schema_uri=_META_FINDING_URI,
        nats_subject_pattern="analyst.{analyst_id}.meta_finding",
    ),
    OutputKind.CRITIQUE: OutputKindSpec(
        kind=OutputKind.CRITIQUE,
        table="analyst_outputs",
        payload_model=CritiquePayload,
        schema_uri=_CRITIQUE_URI,
        nats_subject_pattern="analyst.{analyst_id}.critique",
    ),
    OutputKind.FACT: OutputKindSpec(
        kind=OutputKind.FACT,
        table="facts",
        payload_model=FactPayload,
        schema_uri=_FACT_URI,
        nats_subject_pattern="analyst.{analyst_id}.fact",
    ),
    OutputKind.NEXUS: OutputKindSpec(
        kind=OutputKind.NEXUS,
        table="nexuses",
        payload_model=NexusPayload,
        schema_uri=_NEXUS_URI,
        nats_subject_pattern="analyst.{analyst_id}.nexus",
    ),
    OutputKind.PROMPT_MODULE_CANDIDATE: OutputKindSpec(
        kind=OutputKind.PROMPT_MODULE_CANDIDATE,
        table="analyst_outputs",
        payload_model=PromptModuleCandidatePayload,
        schema_uri=_PROMPT_MODULE_CANDIDATE_URI,
        # NATS subject — optimizer.<optimizer_analyst_id>.candidate.
        # The optimizer analyst_id is the *parent* analyst's optimizer,
        # not the analyst being optimized (the analyst_id placeholder is
        # the optimizer's own id at write time per write_analyst_output).
        nats_subject_pattern="analyst.{analyst_id}.prompt_module_candidate",
    ),
    OutputKind.JOURNAL: OutputKindSpec(
        kind=OutputKind.JOURNAL,
        table="journal_entries",                      # dedicated table — NOT analyst_outputs
        payload_model=JournalPayload,
        schema_uri=_JOURNAL_URI,
        # META analyst: target_id is None → renders as `_`; the subject omits
        # target_id, so the {target_id}-less pattern is correct (plan §3.4).
        nats_subject_pattern="analyst.{analyst_id}.journal",
    ),
    OutputKind.SCORECARD: OutputKindSpec(
        kind=OutputKind.SCORECARD,
        table="analyst_outputs",              # generic table (NOT dedicated)
        payload_model=ScorecardPayload,
        schema_uri=_SCORECARD_URI,
        # META producer: one side-written row per active G20 country. The
        # {target_id}-less subject mirrors the journal pattern.
        nats_subject_pattern="analyst.{analyst_id}.scorecard",
    ),
}


def spec_for_kind(kind: OutputKind | str) -> OutputKindSpec:
    if isinstance(kind, str):
        try:
            kind = OutputKind(kind)
        except ValueError as exc:
            raise KeyError(f"unknown output kind: {kind!r}") from exc
    try:
        return KIND_REGISTRY[kind]
    except KeyError as exc:
        raise KeyError(f"no registered spec for kind {kind!r}") from exc


def register_kind(spec: OutputKindSpec, *, overwrite: bool = False) -> None:
    """Register a new output kind (Phase 6 analyst kinds; L-101 §8 vocab)."""
    if spec.kind in KIND_REGISTRY and not overwrite:
        raise ValueError(
            f"kind {spec.kind!r} already registered; pass overwrite=True to replace"
        )
    KIND_REGISTRY[spec.kind] = spec
