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
    MetaFindingPayload,
    NexusPayload,
    PredictionPayload,
    PromptModuleCandidatePayload,
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
