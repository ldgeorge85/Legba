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
from typing import Any, Mapping, Type

from pydantic import BaseModel

from .models import (
    AlertPayload,
    CritiquePayload,
    EventPayload,
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
    SituationUpdatePayload,
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
    # The 13th kind — CONTINUITY PHASE 2's dated trajectory read. Lands in the
    # generic `analyst_outputs` table, one row per `situation_tracker` cycle,
    # carrying that cycle's delta claim for every open situation that picked up
    # new verified evidence. Goes through the FULL faithfulness verify gate via
    # the composition (sub-claim) citation bridge: "this escalates the situation
    # we were already watching" is a claim about the world, and the ledger rows
    # in `situation_events` point back at THIS row for their grading.
    SITUATION_UPDATE = "situation_update"
    # The 14th kind — DATA MODEL V3 / P0's bounded occurrence. Lands in the
    # dedicated `events` table (migration 0202), keyed by
    # (event_signature, analyst_id). Every live write is gated behind
    # LEGBA_EVENTS (writes._insert_event refuses while it is off); P0 ships no
    # consumer — nothing reads this kind yet.
    EVENT = "event"


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
    # A-1 (ATTENTION_MEASUREMENT_DESIGN §3.1) — the attention instrument is an
    # INSTRUMENT, not a claim about the world, and it must never enter the
    # verify/judge population: this design would otherwise itself add to the
    # ~84% of LLM calls that are the system watching itself. It is also required
    # here by the drift guard, which asserts this set EQUALS the FINDING-emitting
    # deterministic sub-handlers.
    "desk_reference",
    # R-D: the research program's three counters. Pure SQL arithmetic over the
    # research signals — there is no model prose in the row to grade, and its
    # own honest-null discipline (every rate beside its n, null below gate G9's
    # floor) is a stronger statement than a faithfulness score would be. Added
    # here because OUTPUT_KIND_BY_SUB_HANDLER marks it FINDING and this set
    # MUST stay equal to the FINDING emitters; it changes no existing analyst's
    # verify routing.
    "research_measurement",
    # G1 (LEDGER_RESET_2026-09-16 §3, Program 2): the correctness grader is an
    # INSTRUMENT. Its receipt reports what the instrument did — which units it
    # graded, against which reference, under which calibration, at what cost —
    # and asserts nothing about the world that a faithfulness judge could grade.
    # Routing it into the verify population would also be circular in the worst
    # way: the pass that measures groundedness would be scoring the output of
    # the pass that exists because groundedness is not truth. Added here because
    # OUTPUT_KIND_BY_SUB_HANDLER marks it FINDING and this set MUST stay equal
    # to the FINDING emitters.
    "correctness_grader",
    # R2 (LEDGER_RESET_2026-09-16 §3, Program 2): the reference builder is the
    # correctness grader's other half and the same argument binds. Its receipt
    # reports what the instrument did — which target it built, how many
    # committed developments survived which fence, which dimensions came out
    # thin, what it cost the core plane — and asserts nothing about the world a
    # faithfulness judge could grade. The REFERENCE it writes is deliberately
    # not a finding at all: it is an INPUT to a measurement, and routing it
    # through the pass that scores our prose against our own citations is
    # exactly the circularity the reference exists to escape. Added here
    # because OUTPUT_KIND_BY_SUB_HANDLER marks it FINDING and this set MUST
    # stay equal to the FINDING emitters.
    "reference_builder",
    # H12 (Program 5 lane 1, PROGRAM5_INQUIRY_DESIGN_2026-09-24.md §4):
    # inquiry_yield is a deterministic weekly COUNT over the inquiry_ledger —
    # hypotheses opened/confirmed/refuted/expired, questions dispatched/
    # answered, observations later carried forward, blind spots — no model
    # prose in the row to grade. Added here because OUTPUT_KIND_BY_SUB_HANDLER
    # marks it FINDING and this set MUST stay equal to the FINDING emitters.
    "inquiry_yield",
    # Program 6 L2: the layer-divergence unit is an INSTRUMENT over counts.
    # Its finding asserts arithmetic — this layer carried N folded dispatches
    # on this day, that ratio sits Z MAD-scaled deviations from its own
    # fortnight — and every sentence of its body is generated FROM those
    # numbers, so there is no model prose in the row for a faithfulness judge
    # to grade. It does carry re-derivable identities, which is why it also
    # joins STRUCTURAL_CLAIMS_VERIFY_ANALYSTS below: the honest badge and a
    # REAL re-derivation, rather than the badge alone. Added here because
    # OUTPUT_KIND_BY_SUB_HANDLER marks it FINDING and this set MUST stay equal
    # to the FINDING emitters.
    "layer_divergence",
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
    # Program 6 L2 — the layer-divergence unit asserts two identities that are
    # re-derivable from its own payload with no DB access: the divergence
    # partition (total = widening + narrowing, true by construction of
    # `evaluate_pair`'s single direction label) and the distinct-desk count
    # over its per-desk receipts. A partition bug in either would surface as a
    # flagged critique rather than as a number nobody checked.
    "layer_divergence",
})


# ---------------------------------------------------------------------------
# D-5 (DEMOTION_D1_SPEC_2026-09-04 §4) — the DETERMINISTIC ROLLUP registry
# ---------------------------------------------------------------------------
# Under the assembly regime ``region_composition`` stops generating prose and
# emits a ``region_rollup.v1`` payload: no LLM, no prompt, no faithfulness
# judge. It therefore needs the same two things a structural analyst needs —
# an honest badge instead of a silent nothing, and a REAL deterministic
# verification of the numbers it asserts.
#
# WHY THIS IS A SEPARATE SET AND NOT A MEMBER OF THE TWO ABOVE. §4.3 item 5
# proposed adding ``region_composition`` to both. It cannot go in either:
#
#   * ``STRUCTURAL_VERIFY_EXEMPT_ANALYSTS`` is drift-guarded to be EQUAL to the
#     FINDING-emitting deterministic SUB-HANDLERS (test_trace_only_output_split
#     ``test_structural_verify_exempt_registry_matches_finding_sub_handlers``).
#     ``region_composition`` is a ``meta_findings_synthesizer``, not a
#     sub-handler. Adding it makes that guard assert something false, and the
#     guard is load-bearing: it is what keeps the badge registry honest as
#     handlers come and go.
#   * ``STRUCTURAL_CLAIMS_VERIFY_ANALYSTS`` is guarded to be a SUBSET of the
#     first, so it inherits the same problem.
#
# And the deeper reason: those sets are claims about an ANALYST ("this producer
# is deterministic, always"). A region row's determinism is a property of the
# REGIME IT WAS WRITTEN UNDER — the same analyst_id produced graded LLM prose
# last week. A set that means "always deterministic" cannot hold an id that is
# only sometimes deterministic without becoming a lie in one direction or the
# other. So the ANALYST joins its own registry, and the ROW carries the
# discriminator (:func:`is_deterministic_rollup`).
DETERMINISTIC_ROLLUP_ANALYSTS: frozenset[str] = frozenset({
    "region_composition",
})

#: The finding-``data`` key + schema that MARK a row as a deterministic rollup.
#: Declared here, in the verify-registry module, and imported by the producer
#: (``data.analysts.region_rollup``) rather than the other way round — every
#: guard that must recognise a rollup lives on this side of the layering, and
#: none of them may import an analyst module to do it.
ROLLUP_PAYLOAD_KEY: str = "rollup"
ROLLUP_PAYLOAD_SCHEMA: str = "region_rollup.v1"

#: ``members[].lead_source`` for a member whose lead block WAS carried — the
#: token that selects the rollup's rendered member sections, in order.
#:
#: Declared here for the same layering reason as the two above, and needed on
#: this side since the 2026-09-07 citation-order fix: the EXPORT re-maps a
#: historical rollup row's citations onto the order the body actually renders
#: (``export_api._rollup_aligned_citations``), and a read-side guard may not
#: import an analyst module to learn the token it filters on. The producer
#: aliases it (``region_rollup.LEAD_CARRIED``).
ROLLUP_LEAD_CARRIED: str = "carried"

#: The badge a rollup row carries. NOT ``"structural"``: a reader who is told
#: "structural" goes looking for a mining/aggregate handler and finds a
#: composition analyst, which is a worse answer than no answer. This names what
#: actually happened.
ROLLUP_EXEMPT_REASON: str = "deterministic-rollup"


def is_deterministic_rollup(data: Any) -> bool:
    """True iff a finding's ``data`` carries a ``region_rollup.v1`` payload.

    THE REGIME DISCRIMINATOR, and it reads the ROW rather than the environment.
    A row composed under the rollup regime keeps its own semantics forever,
    including after the flag flips back; an env read here would retroactively
    re-label history, which is the 08-12 pooling failure in a new costume.

    Accepts either the ``analyst_outputs.data`` envelope (payload at
    ``data.data.rollup``) or the inner payload dict directly, because the
    callers sit on both sides of that boundary.
    """
    if not isinstance(data, Mapping):
        return False
    for candidate in (data.get("data"), data):
        if not isinstance(candidate, Mapping):
            continue
        rollup = candidate.get(ROLLUP_PAYLOAD_KEY)
        if (
            isinstance(rollup, Mapping)
            and str(rollup.get("schema") or "") == ROLLUP_PAYLOAD_SCHEMA
        ):
            return True
    return False


def rollup_exempt_reason(analyst_id: str | None, data: Any = None) -> str | None:
    """The verify-exemption tag for a DETERMINISTIC ROLLUP row, or ``None``.

    Both conditions are required — the analyst must be registered above AND the
    row must actually carry the payload — so a legacy generative region read
    keeps its ordinary "unverified / verified" semantics untouched, and a stray
    ``rollup`` key on some other analyst's row never earns a badge.
    """
    if analyst_id is None or analyst_id not in DETERMINISTIC_ROLLUP_ANALYSTS:
        return None
    return ROLLUP_EXEMPT_REASON if is_deterministic_rollup(data) else None


def structural_claims_verify_opt_in(analyst_id: str | None) -> bool:
    """Whether ``analyst_id`` opts into the deterministic structural_claims
    verify profile (C2b). False for every non-opted-in analyst.

    D-5 widens this to :data:`DETERMINISTIC_ROLLUP_ANALYSTS`, and the widening
    is SAFE WITHOUT A FLAG READ because of the profile's own contract: *"An
    opted-in analyst whose finding carries no ``data['structural_claims']``
    block is a NO-OP (no critique written; the row keeps its honest badge)."*
    A legacy generative region read declares no claims → no critique → the
    flag-off path is byte-identical. A rollup declares four → they are
    re-derived. The REGIME gates itself, on the row, with nothing to configure.
    """
    return analyst_id is not None and (
        analyst_id in STRUCTURAL_CLAIMS_VERIFY_ANALYSTS
        or analyst_id in DETERMINISTIC_ROLLUP_ANALYSTS
    )


#: The badge a rollup row carries once its arithmetic has been re-derived and
#: PASSED. Mirrors the ``structural`` / ``structural-verified`` pair, so the
#: reader's question ("was anything actually checked?") gets the same two-state
#: answer everywhere it is asked.
ROLLUP_VERIFIED_REASON: str = "deterministic-rollup-verified"


def structural_badge(
    analyst_id: str | None,
    structural_verified: bool | None,
    data: Any = None,
) -> str | None:
    """The ``verify_exempt`` badge stamp, folding a structural verdict (C2b).

    Extends :func:`verify_exempt_reason`: a structural finding that now carries a
    PASSING structural critique (``structural_verified is True``) reads
    ``"structural-verified"``; one without (or a failed / unverifiable verdict)
    keeps the honest ``"structural"`` (rendered ``unverified — structural``).
    ``None`` for every non-structural analyst — never fabricated.

    D-5 adds the ROLLUP pair on the same two-state pattern. ``data`` is optional
    and defaults to ``None``, so every existing call site keeps its exact
    behaviour: a caller that does not pass the row cannot produce a rollup badge
    and falls through to the structural branch unchanged.
    """
    rollup = rollup_exempt_reason(analyst_id, data)
    if rollup is not None:
        return ROLLUP_VERIFIED_REASON if structural_verified is True else rollup
    base = verify_exempt_reason(analyst_id)
    if base == "structural" and structural_verified is True:
        return "structural-verified"
    return base


# QW1-B — the DESK GROUNDING citation vocabulary.
#
# A bounded unit's ``data['citations']`` used to hold exactly one entry shape:
# a ``[N]`` marker bound to a ``signals`` row id. QW1-B adds four MORE citable
# block kinds (see :mod:`legba.data.analysts.unit_grounding`), FRAME-2 a fifth
# and V3/P2 a sixth, none of which is a signal: this unit's own PRIOR READ (a real
# ``analyst_outputs`` uuid, carried as ``ref_id``), and four SYNTHETIC blocks —
# the WINDOW LEDGER, the open-situation REGISTER, the DESK BASELINE and the
# STANDING OPEN QUESTIONS — which have no single substrate id and therefore
# carry the REAL underlying ids (``ledger_finding_ids`` / ``situation_ids`` /
# ``baseline_keys`` / ``question_ids``) and NO ``ref_id`` at all. Minting a
# ``ref_id`` so a drill link resolves would be a fabricated anchor.
#
# THE SET LIVES HERE, not in the producing module, for ONE reason: the CONSUMER is
# ``provenance.verify``, and ``verify`` importing ``data.analysts.inline_target``
# would close an import cycle (``runtime.analyst_method`` → ``inline_target``).
# This module is already imported by both sides, so one definition serves both and
# no drifting copy is created.
#
# NOTE what is deliberately ABSENT: ``'finding'``. That token is the COMPOSITION
# discriminator (``verify._uses_subclaim_convention``); stamping it on a unit's
# prior-read citation would route the whole unit finding to the sub-claim verify
# floor. The prior read gets its own ``ref_kind`` instead.
#
# ALSO ABSENT, BY DESIGN (V3/P2): ``'event'``. An event citation is EXPANDED at
# build time into ordinary per-signal entries — each carries a ``signal_id``,
# so :func:`is_grounding_citation` correctly returns False on it and the judge
# grounds on the signals' raw source text. Admitting ``event`` here would let
# an event be cited with ``evidence_text`` set to the event's OWN summary —
# the exact rubber-stamp DATA_MODEL_V3 §2.5 rules 1–2 exist to prevent.
GROUNDING_REF_KINDS: frozenset[str] = frozenset({
    "prior_read",
    "situation_register",
    "desk_baseline",
    "open_questions",
    # V3/P2 (2026-09-24) — OPEN EVENTS, the sixth block and the OFFER half of
    # the event ref kind: the desk's live ``events``, reached through its open
    # ``situations``, each line carrying the event's ``event:<uuid>`` TOKEN so
    # a unit can cite the occurrence's underlying REPORTS. Registered here for
    # the same reason the other five are — without it the verify path scores an
    # events-block-backed clause as an unresolved citation and false-demotes a
    # read that cited exactly what it was shown.
    #
    # READ THIS BESIDE THE ``'event'`` PARAGRAPH ABOVE — they are two different
    # things and keeping them apart is the whole design. ``'open_events'`` is a
    # GROUNDING BLOCK: no ``signal_id``, graded on its own rendered
    # ``evidence_text``, which is a bounded list of titles/spans/counts and
    # NEVER an event summary. ``'event'`` is what an EXPANDED citation carries:
    # it has a ``signal_id``, is graded on the report's raw source text, and
    # stays out of this set forever. Admitting ``'event'`` here — or rendering
    # a summary into the block's text — is the same rubber-stamp by two routes.
    "open_events",
    # 7g-2 (2026-09-25) — the OBSERVATION: ONE row of a curated historical
    # holding (`observations`), cited by the ordinal its line took in the
    # HISTORICAL SERIES grounding block, or returned by the `series_history` /
    # `series_compare` pack tools and cited by the consult/research loop.
    #
    # WHY IT BELONGS HERE AND NOT BESIDE ``'event'``. An observation entry
    # carries NO ``signal_id`` — an observations row is not a signals row, has
    # no article behind it and no outlet — and it IS graded on its own
    # captured ``evidence_text``, which is the deterministic rendering of the
    # row itself: provider, series, subject, the VALUE with its unit, the
    # valid period, the record time, the source URL and the sha256 of the file
    # the number was read out of. That text is not a summary of evidence, it
    # IS the evidence, which is exactly the property ``'event'`` lacks (an
    # event's own summary is never evidence, §2.5) and exactly why ``'event'``
    # stays out of this set while this one belongs in it.
    #
    # Registered here for the same reason all six above are: without it the
    # verify path scores an observation-backed clause as an unresolved
    # citation and false-demotes a read that cited a number it was actually
    # shown — and the judge would be handed nothing to grade the PERIOD
    # against, which is the one thing a historical figure must be graded on.
    #
    # UNLIKE the five synthetic blocks, an observation DOES carry ``ref_id``:
    # an observations row has a real, single uuid, so pointing at it is not a
    # fabricated anchor but the honest drill target.
    "observation",
    # FRAME-2 (2026-08-20) — the WINDOW LEDGER, the fifth block: a bounded,
    # dated record of the verified severity-tagged heads this scope itself
    # produced over the trailing fortnight. Same synthetic shape as the register
    # (no ``ref_id``; the REAL member uuids ride ``ledger_finding_ids``), and it
    # is registered HERE for the same reason the other four are — without it the
    # verify path would score a ledger-backed clause as an unresolved citation
    # and false-demote the exact carry the train exists to license.
    "window_ledger",
})


def is_grounding_citation(entry: object) -> bool:
    """True iff a citation entry is a DESK GROUNDING block, not a cited signal.

    The discriminator the verify path uses to admit a block-backed clause: an
    entry with NO ``signal_id``, a ``ref_kind`` in :data:`GROUNDING_REF_KINDS`,
    and real captured ``evidence_text``. All three are required — an entry
    missing its evidence text is not gradeable and must NOT count as support.
    """
    if not isinstance(entry, Mapping):
        return False
    if entry.get("signal_id"):
        return False
    if entry.get("ref_kind") not in GROUNDING_REF_KINDS:
        return False
    text = entry.get("evidence_text")
    return isinstance(text, str) and bool(text.strip())

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
# Continuity P2 trajectory read — generic `analyst_outputs` table (no dedicated
# table / DB default), so the URI is declared here only.
_SITUATION_UPDATE_URI = "iglu:legba/situation_update/jsonschema/1-0-0"
# Matches the DB default on `events.schema_uri` (0202_events.sql).
_EVENT_URI         = "iglu:legba/event/jsonschema/1-0-0"


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
    OutputKind.SITUATION_UPDATE: OutputKindSpec(
        kind=OutputKind.SITUATION_UPDATE,
        table="analyst_outputs",           # generic table (NOT `situations`)
        payload_model=SituationUpdatePayload,
        schema_uri=_SITUATION_UPDATE_URI,
        # META producer (a global sweep over open situations): target_id is None
        # → renders as `_`, so the {target_id}-less subject is correct (the
        # journal / scorecard pattern).
        nats_subject_pattern="analyst.{analyst_id}.situation_update",
    ),
    OutputKind.EVENT: OutputKindSpec(
        kind=OutputKind.EVENT,
        table="events",                       # dedicated table — NOT analyst_outputs
        payload_model=EventPayload,
        schema_uri=_EVENT_URI,
        nats_subject_pattern="analyst.{analyst_id}.event",
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
