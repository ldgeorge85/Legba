# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pydantic payload models per output kind.

These are the *payload* shapes — distinct from the substrate-row columns. A
write helper merges payload + provenance fields into the row. Schemas use
``model_config = ConfigDict(extra="forbid")`` so an analyst with a stale
schema_uri trips a clear ValidationError → DLQ per L-107 §6.

Versioning per L-101 §7: each model corresponds to a major.minor.patch
declared in ``kinds.py``. Adding a backwards-compatible field bumps minor;
removing or renaming bumps major and demands a conversion-webhook
registration (L-112).

These v1 shapes are deliberately minimal — Phase 6 analyst handlers will
expand fields with backwards-compatible bumps as concrete needs surface.
The schemas exist now so Phase 1 write helpers can refuse malformed payloads
and route them to ``output_dead_letter``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Source-kind ingestion (target writes)
# ---------------------------------------------------------------------------


class SignalPayload(BaseModel):
    """Source-kind output written via ``write_target_signal``.

    Mirrors the hot columns on ``signals`` so write_target_signal can split
    them into typed columns + JSONB ``data``. The full descriptor-driven
    enrichment lands in Phase 3 (source handlers); this is the minimum
    contract.
    """

    model_config = ConfigDict(extra="allow")

    title: str = Field(min_length=1, max_length=2048)
    source_url: str = Field(default="", max_length=4096)
    guid: str = Field(default="", max_length=512)
    category: str = Field(default="other")
    event_timestamp: datetime | None = None
    language: str = Field(default="en", max_length=16)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_id: UUID | None = None
    # Descriptor-binding identity for the source that produced this signal
    # (e.g. `agbrasil_econ_rss`). Distinct from `source_id` which is a UUID
    # FK into the legacy `sources` table — kept for that future linkage but
    # rarely populated today. Always present from the runtime path; empty
    # string for legacy / hand-injected signals.
    descriptor_source_id: str = Field(default="", max_length=256)
    classification_scores: dict[str, float] | None = None
    data: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Analyst-output kinds
# ---------------------------------------------------------------------------


class _AnalystOutputBase(BaseModel):
    """Shared shape for the four kinds that share the generic table."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=2048)
    body: str = Field(default="", max_length=65536)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)


class FindingPayload(_AnalystOutputBase):
    """Generic analyst observation — first-order output of inline/cross-target."""

    kind_marker: Literal["finding"] = "finding"


class MetaFindingPayload(_AnalystOutputBase):
    """Higher-order synthesis across other analysts' findings."""

    kind_marker: Literal["meta_finding"] = "meta_finding"
    contributing_analysts: list[str] = Field(default_factory=list)


class ScorecardPayload(_AnalystOutputBase):
    """P4-T2 banded per-country verdict — the HONEST top of the system.

    A *perspective over* already-verified sub-claims: one row per active G20
    country (``kind='scorecard'``, generic ``analyst_outputs`` table), whose
    ``data.bands`` carries the T1 :func:`scorecard_banding.band_target` verdict
    VERBATIM (per-dimension band / basis / eval / reason + the composition node).
    Its ``derived_from`` NAMES exactly the verified basis findings the bands rest
    on, so a P1 lineage walk resolves them with zero dangling. All scorecard
    structure lives inside the free-form inherited ``data`` dict so
    ``extra='forbid'`` at the top level never rejects the bands.
    """

    kind_marker: Literal["scorecard"] = "scorecard"


class AlertPayload(_AnalystOutputBase):
    """Operator-routed alert (severity-gated, NATS-emitted)."""

    kind_marker: Literal["alert"] = "alert"
    severity: Literal["info", "low", "medium", "high", "critical"] = "medium"
    routing_hint: str = Field(default="", max_length=256)


class CritiquePayload(_AnalystOutputBase):
    """Critique payload — covers both narrative critique outputs and the
    L-175 eval-loop critic's scored judgments.

    Historical (Wave A) usage: analyst-kind outputs whose primary product
    is a narrative critique (``target_ref`` + free-text ``rubric``).

    L-175 eval-loop usage (added 2026-05-21): the critic analyst kind
    grades another analyst's output against the analyzed analyst's
    ``descriptor.eval.rubric`` block and emits a scored ``CritiquePayload``
    per L-105 §3.2. The L-105 fields are optional so the historical Wave A
    shape (used by ``write_critique`` in ``provenance/writes.py`` and
    ``test_writes.test_write_critique_lands_row``) keeps validating
    unchanged.

    Heterogeneity guard enforcement lives in the kind module
    (:mod:`legba.data.analysts.critic`) — the payload just carries the
    audit fields. ``judge_model`` ≠ ``analyzed_model`` is the contract;
    the payload itself doesn't validate the inequality so a deliberate
    ``allow_self_correlated`` escape-hatch (per L-105) can still land a
    row with the two equal.
    """

    kind_marker: Literal["critique"] = "critique"

    # Wave A narrative-critique fields (kept for back-compat).
    target_ref: UUID | None = None
    rubric: str = Field(default="", max_length=256)

    # L-175 eval-loop fields (per L-105 §3.2). All optional so the Wave A
    # shape ``CritiquePayload(title=..., target_ref=...)`` still validates.
    analyzed_output_id: UUID | None = None
    analyzed_analyst_id: str = Field(default="", max_length=256)
    analyzed_analyst_version: str = Field(default="", max_length=64)
    analyzed_model: str = Field(default="", max_length=128)
    judge_model: str = Field(default="", max_length=128)
    scores: dict[str, float] = Field(default_factory=dict)
    overall_score: float | None = Field(default=None, ge=0.0, le=1.0)
    revision_delta: str | None = Field(default=None, max_length=8192)


class ConsultResponsePayload(BaseModel):
    """Structured synthesis from the `consult_on_demand` analyst kind (L-178).

    Added per L-178 — preserves the heavily-used legacy ConsultPanel
    capability as the 9th analyst-kind output shape. The kind carries this
    payload via ``FindingPayload.data`` (so the existing
    ``OutputKind.FINDING`` write path stays compatible) and also returns
    it directly to non-runtime dispatchers (A2A skill, MCP tool, future
    operator panel).
    """

    model_config = ConfigDict(extra="forbid")

    kind_marker: Literal["consult_response"] = "consult_response"
    question: str = Field(min_length=1, max_length=8192)
    answer: str = Field(default="", max_length=65536)
    cited_substrate_refs: list[UUID] = Field(default_factory=list)
    uncertainty: float = Field(default=0.5, ge=0.0, le=1.0)
    unanswered_aspects: list[str] = Field(default_factory=list)
    # Free-form bag for things like tool-call trace summary, model name, etc.
    data: dict[str, Any] = Field(default_factory=dict)


class PromptModuleCandidatePayload(BaseModel):
    """L-176 optimizer output — a candidate ``prompt_module`` for another analyst.

    The optimizer kind compiles a new candidate via DSPy + GEPA over an
    analyst's joined ``analyst_traces`` + ``analyst_critiques`` rows, then
    writes one of these payloads to ``analyst_outputs`` with
    ``kind = 'prompt_module_candidate'`` (per L-176 §Output payload).
    Promotion to the parent analyst's live prompt_module is gated
    downstream by :func:`legba.data.analysts.optimizer.should_auto_promote`
    against the descriptor's ``eval.promotion`` policy.

    Lands in the generic ``analyst_outputs`` table — no new migration
    required; the runtime's existing dispatch path persists the row
    unchanged (per the brief: "lean toward analyst_outputs with the new
    OutputKind for consistency").
    """

    model_config = ConfigDict(extra="forbid")

    kind_marker: Literal["prompt_module_candidate"] = "prompt_module_candidate"

    # Identity of what was optimized (the *parent* analyst, not the
    # optimizer itself — the optimizer analyst's id lives in the usual
    # universal-provenance columns on the row).
    analyst_id: str = Field(min_length=1, max_length=256)
    analyst_version: str = Field(min_length=1, max_length=256)

    # Parent prompt_module import path (e.g. ``legba.prompts.inline_target.v1``)
    # + the candidate's proposed body.  The body is whatever the GEPA loop
    # surfaces — typically the new instruction text of the DSPy predictor's
    # signature, but kinds whose prompt is a richer composite store the
    # full serialized form so an operator can diff parent vs candidate.
    parent_prompt_module_path: str = Field(min_length=1, max_length=512)
    candidate_prompt_module_text: str = Field(min_length=1, max_length=131072)

    # Snapshot of the PARENT prompt-module text as it was at compile time —
    # the baseline the candidate's ``eval_score_delta`` was measured against.
    # Captured here so the operator diff route (``GET .../candidates/{id}/diff``)
    # can show current-vs-candidate WITHOUT re-importing the prompt module
    # (the diff route must never import dspy / the prompt package — that work
    # only happens inside the opt-in GEPA worker). Defaults empty: rows written
    # before this field existed degrade to "" and the diff route falls back to
    # the live promoted-prompt lookup. Capped to the same 128KiB ceiling as the
    # candidate body so a pathological prompt can't bloat the row.
    parent_prompt_module_text: str = Field(default="", max_length=131072)

    # Training-set provenance.
    training_set_size: int = Field(ge=0)

    # Holdout evaluation.  ``eval_score_delta`` is candidate − parent on
    # the same holdout; negative deltas are still valid rows (promotion
    # gating filters them out, but the row remains for audit).
    eval_score: float = Field(ge=0.0, le=1.0)
    eval_score_delta: float = Field(ge=-1.0, le=1.0)

    # GEPA generation counter (0 = the parent itself, >=1 = evolved).
    gepa_generation: int = Field(ge=0, default=0)

    # Promotion gating per the 2026-05-16 ratified decision:
    #   * ``human_gated``        — operator review required (default).
    #   * ``auto_with_threshold``— eligible for auto-promotion after the
    #                              5-successful-manual-promotions threshold
    #                              has been met for this analyst_id.
    #   * ``rejected``           — post-hoc operator rejection (kept for
    #                              audit; the optimizer won't re-evolve from
    #                              a rejected candidate).
    promotion_gate: Literal["human_gated", "auto_with_threshold", "rejected"] = (
        "human_gated"
    )

    # Temporal workflow / run identifiers for replay + debug.  Empty
    # strings on the no-Temporal fallback path (used by tests + the
    # default-test-env path where Temporal isn't running).
    temporal_workflow_id: str = Field(default="", max_length=256)
    temporal_run_id: str = Field(default="", max_length=256)

    # Free-form bag — baseline (parent) score, per-generation scores,
    # best-of-N stats, model name, judge_analyst_id used for scoring, etc.
    data: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Dedicated-table kinds
# ---------------------------------------------------------------------------


class SituationPayload(BaseModel):
    """Mirrors hot columns on the ``situations`` table."""

    model_config = ConfigDict(extra="allow")

    name: str = Field(min_length=1, max_length=512)
    status: str = Field(default="active")
    category: str = Field(default="")
    intensity_score: float = Field(default=0.0, ge=0.0)
    event_count: int = Field(default=0, ge=0)
    last_event_at: datetime | None = None
    # First-class situation identity + temporal frame (Phase 5a, migration 0040).
    # ``situation_signature`` is the upsert key (with the row's analyst_id) for the
    # standard write path; ``valid_from``/``valid_until`` make a situation a
    # persistent FRAME ("active over [t0, t1)") rather than a mutable snapshot.
    situation_signature: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class HypothesisPayload(BaseModel):
    """Mirrors hot columns on the ``hypotheses`` table."""

    model_config = ConfigDict(extra="allow")

    thesis: str = Field(min_length=1, max_length=4096)
    counter_thesis: str = Field(default="", max_length=4096)
    diagnostic_evidence: list[Any] = Field(default_factory=list)
    supporting_signals: list[UUID] = Field(default_factory=list)
    refuting_signals: list[UUID] = Field(default_factory=list)
    evidence_balance: int = Field(default=0)
    status: str = Field(default="active")
    situation_id: UUID | None = None


class PredictionPayload(BaseModel):
    """Mirrors hot columns on the ``predictions`` table."""

    model_config = ConfigDict(extra="allow")

    hypothesis: str = Field(min_length=1, max_length=4096)
    source_cycle: int = Field(default=0, ge=0)
    source_type: str = Field(default="report")
    category: str = Field(default="")
    region: str = Field(default="")
    status: str = Field(default="open")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence_for: list[str] = Field(default_factory=list)
    evidence_against: list[str] = Field(default_factory=list)


class FactPayload(BaseModel):
    """Mirrors the hot columns on the ``facts`` table (altitude-0 extraction).

    Carries one ``(subject, predicate, value)`` triple plus its temporal /
    geo / confidence metadata. Written by:

      * the ingest-time ``fact_extractor`` enrichment stage
        (``source_type='ingestion'`` — source-owned, no analyst_id), and
      * analyst- / workflow-emitted facts via ``write_fact`` (the Piece 4
        synthesize stage), which carry the usual provenance columns.

    Mirrors ``HypothesisPayload``'s ``extra="allow"`` (a fact may carry
    producer-specific extras); the ``kind_marker`` follows the
    ``FindingPayload`` family so a fact wrapped in a finding envelope is
    distinguishable. The shape stands on its own — it is NOT derived from
    ``HypothesisPayload``.
    """

    model_config = ConfigDict(extra="allow")

    subject: str = Field(min_length=1, max_length=2048)
    predicate: str = Field(min_length=1, max_length=512)
    value: str = Field(min_length=1, max_length=4096)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_type: str = Field(default="agent")        # 'ingestion' for the stage
    source_cycle: int | None = None
    valid_from: datetime | None = None
    # Creation-time forward TTL / curated expiry — a DIFFERENT thing from the
    # supersession close (which the engine stamps valid_until=now() at
    # supersede). Carried by curated seeds (a leader's term-end) and persisted
    # on insert; NULL = open-ended.
    valid_until: datetime | None = None
    geo_lat: float | None = None
    geo_lon: float | None = None
    evidence_set: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    kind_marker: Literal["fact"] = "fact"


class JournalClaim(BaseModel):
    """Per-claim citation binding (plan §3.6).

    A flat row-level ``cited_substrate_refs`` cannot tell the UI which sentence
    each ref backs, so the "every claim a chip" promise would degrade to a
    footnote pile. The body carries inline ``[[ref:<uuid>]]`` markers the UI
    resolves to chips at the cited span; this sidecar lets the UI bind chips to
    spans without re-parsing markdown.

    ``kind='perspective'`` claims may carry ZERO refs (wonder / inference, not a
    factual assertion — §4.5). ``kind='fact'`` claims carry ≥1 ref OR an explicit
    speculation marker (the writer flags an uncited factual span; it is never
    auto-deleted — voice-preservation is the tie-breaker).
    """

    model_config = ConfigDict(extra="forbid")

    text_span: str = Field(min_length=1, max_length=8192)
    refs: list[UUID] = Field(default_factory=list)
    kind: Literal["fact", "perspective"] = "fact"


class JournalPayload(BaseModel):
    """The journal's one output payload — an ``entry`` or a ``consolidation``
    (plan §3.2 / §8). Lands in the dedicated ``journal_entries`` table, OFF the
    fact/finding/nexus chain (§3.1). NOT a fact source.

    ``extra='forbid'`` (mirrors the dedicated-table kinds' contract for a stable
    payload shape — changing the shape later is a migration, so it is pinned now,
    §3.6). The temporal-supersession columns (``valid_from``/``valid_until``/
    ``superseded_by``) are stamped by ``_insert_journal_entry`` +
    ``supersede_prior_consolidation``, NOT carried on the payload (mirrors how
    facts/nexuses stamp their lifecycle).
    """

    model_config = ConfigDict(extra="forbid")

    # The discriminator. Entries are pure append; only consolidations supersede.
    entry_kind: Literal["entry", "consolidation"] = "entry"
    title: str = Field(min_length=1, max_length=2048)
    # The narrative (markdown, with inline [[ref:<uuid>]] citation markers).
    body: str = Field(default="", max_length=65536)
    # Per-claim ref binding (§3.6) — the chip-to-span source of truth.
    claims: list[JournalClaim] = Field(default_factory=list)
    # Flat union of every ref cited anywhere in the body — query convenience;
    # the BINDING lives in ``claims``.
    cited_substrate_refs: list[UUID] = Field(default_factory=list)
    # The window the entry reflects on.
    period_start: datetime
    period_end: datetime
    # consolidation → prior consolidation; NULL for entries AND for the
    # first-ever consolidation (the bootstrap case, §8).
    supersedes: UUID | None = None
    # Forced DETERMINISTICALLY by a non-LLM post-step (§10) — never trusted as
    # agent-self-reported. Empty in Wave 0 (the deterministic post-step is Wave 1).
    honesty_flags: list[str] = Field(default_factory=list)
    kind_marker: Literal["journal"] = "journal"


class NexusPayload(BaseModel):
    """Mirrors the hot columns on the ``nexuses`` table (PIECE A — reified
    typed relationship).

    Carries one reified relationship — ``subject`` →[``intermediary``]→
    ``object`` — typed by ``rel_type`` with a canonical POLARITY ``polarity``
    sign (+1 supportive / -1 antagonistic / 0 neutral, the structural-balance
    convention) plus ``intent`` (why the intermediation exists) and ``channel``
    (direct / proxy / covert / institutional). ``intermediary`` is ``None`` for
    a direct A→B relationship.

    Written by the ``relationship_reifier`` META analyst kind (the 8B-LLM
    typing producer). Mirrors ``FactPayload``'s ``extra="allow"`` + the facts
    temporal lifecycle (``valid_from``/``valid_until``/``superseded_by`` are
    stamped by ``_insert_nexus`` + ``supersede_prior_nexuses``, not the
    payload). The shape stands on its own — it is NOT derived from
    ``FactPayload``.
    """

    model_config = ConfigDict(extra="allow")

    subject: str = Field(min_length=1, max_length=2048)
    intermediary: str | None = Field(default=None, max_length=2048)
    object: str = Field(min_length=1, max_length=2048)
    rel_type: str = Field(min_length=1, max_length=512)
    label: str = Field(default="", max_length=4096)
    polarity: int = Field(default=0, ge=-1, le=1)
    intent: str = Field(default="", max_length=512)
    channel: str = Field(default="direct", max_length=64)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    valid_from: datetime | None = None
    # Creation-time forward TTL / curated expiry (mirrors FactPayload) — a
    # DIFFERENT thing from the supersession close the engine stamps; persisted
    # on insert, NULL = open-ended.
    valid_until: datetime | None = None
    source_signal_ids: list[UUID] = Field(default_factory=list)
    data: dict[str, Any] = Field(default_factory=dict)
    kind_marker: Literal["nexus"] = "nexus"
