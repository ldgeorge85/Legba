# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tier-1 knowledge grounding — analysis-time current-world-state injection.

Stale-cutoff analyst LLMs (``world_assessor`` / ``country_assessor``) backfill
current world facts (officeholders, alliances, ongoing-conflict state) from a
training prior that predates the present — e.g. calling the CURRENT US
president a "former" one. The signal slice rarely restates such background
facts, so the model has no in-context correction.

Legba already stores the temporally-honest answer in the substrate: curated /
seed ``facts`` rows (``valid_from`` / ``valid_until`` / ``superseded_by``) and
signed ``nexuses``. This module is the *injection* half of the fix (the
descriptor ``grounding`` block is the opt-in; see
:class:`legba.data.schemas.analyst.GroundingBlock`):

  * :class:`SubstrateGroundingResolver` reads the substrate for the CURRENT
    authoritative facts (the temporal-honesty gate
    ``superseded_by IS NULL AND (valid_until IS NULL OR valid_until > now())``,
    RESTRICTED to ``source_type IN ('seed','curated')`` — the provenance gate;
    see below) about the analyst's target geo + the top entities in the signal
    slice; and
  * :func:`build_grounding_preamble` renders them into a single dated
    "AUTHORITATIVE CURRENT CONTEXT (as of <today> — treat as ground truth over
    prior knowledge)" block that the inline_target runner PREPENDS to the LLM
    user prompt.

PROVENANCE GATE (the grounding-quality fix). The preamble header tells the LLM
to "treat as ground truth over any prior knowledge" — so what reaches it must
actually BE ground truth, not an open NER extraction. A live audit found the
ingestion path laundered hallucinated triples that are current + grounding-
eligible (``Iran | capital of | US``, ``Iran | controls | Israel``, and
``Adolf Hitler | leader of | Germany`` at confidence **1.0**). Two lessons:

  * confidence is NOT a usable trust signal for ingestion facts — the relation
    backend (GLiREL) scores a relation's plausibility, not its curated truth,
    so junk like the above can still score high and sit near the top of the
    confidence order (and the historical REBEL backend stamped a synthetic 1.0
    floor that leaked). A ``confidence >= X AND predicate IN <whitelist>``
    Tier-2 would inject "Hitler is the current leader of Germany"; it is unsafe.
  * the authoritative current-world layer the stale cutoff actually needs
    (officeholders, alliances, active conflicts) is exactly what the operator
    curates into the seed. Breaking news rides the signal slice itself.

So the resolver RESTRICTS both facts and signed nexuses to operator-vetted
provenance — ``source_type IN ('seed','curated')`` (env-overridable via
``LEGBA_GROUNDING_TRUSTED_SOURCE_TYPES``). Ingestion / agent rows never reach
the "ground truth" block. This also subsumes the leg-1b coherence filter: an
ingested ``<person> | leader of | <geo>`` can no longer contradict the seeded
officeholder because it is filtered out wholesale, not row-by-row.

Design constraints (per planning/KNOWLEDGE_GROUNDING_PLAN.md Tier 1):

  * NO new vector / embed dependency — this is a couple of cheap Postgres
    reads against the same current-facts gate
    :mod:`legba.runtime.substrate_query_port` already uses. (The
    ``vector:world_context`` source is the declared Tier-2 follow-up.)
  * Token-capped via ``max_facts`` so the preamble can't blow the context.
  * Off unless declared — the resolver is only constructed for analysts whose
    descriptor sets ``grounding.enabled: true`` (the deps-builder gate); a
    resolver handed an empty candidate set returns ``None`` (no preamble) so a
    thin slice never injects a stray header.
  * Degrade-not-drop — any read failure logs + yields ``None`` (no preamble)
    rather than failing the analyst run. Grounding is an enrichment.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

from ..data.facts.decay import fact_decay_weighting_enabled

logger = logging.getLogger(__name__)


__all__ = [
    "GroundingFact",
    "GroundingGraphStructure",
    "GroundingInterestingItem",
    "GroundingNarrative",
    "GroundingNexus",
    "GroundingOpenQuestion",
    "GroundingSituation",
    "GroundingWorldContextChunk",
    "SubstrateGroundingResolver",
    "build_graph_structure_block",
    "build_grounding_preamble",
    "build_narratives_block",
    "build_open_questions_block",
    "build_situations_block",
    "build_world_context_block",
    "collect_grounding_candidates",
    "finding_is_off_target",
    "harvest_class_of",
    "is_non_event_situation_name",
    "open_question_priority_key",
    "situation_grounding_min_intensity",
    "situation_scope_for_target",
    "target_country_name",
    "target_scope_names",
    "trusted_source_types",
    "world_context_country_filter_values",
    "world_context_min_score",
]


# The provenance gate (see the module docstring's PROVENANCE GATE note). Only
# operator-vetted ``source_type`` values reach the "treat as ground truth"
# preamble; ingestion/agent rows are dropped wholesale because confidence is
# not a usable trust signal for them (NER hallucinations land at conf 1.0).
# Env-overridable so an operator who later adds a high-trust source_type (e.g.
# a vetted ``wikidata`` lane distinct from ``seed``) can admit it without a
# code change; an empty/blank override falls back to the safe default.
_DEFAULT_TRUSTED_SOURCE_TYPES: tuple[str, ...] = ("seed", "curated")


def trusted_source_types() -> tuple[str, ...]:
    """The ``source_type`` values admitted into the grounding preamble.

    Reads ``LEGBA_GROUNDING_TRUSTED_SOURCE_TYPES`` (comma-separated,
    case-insensitive); falls back to :data:`_DEFAULT_TRUSTED_SOURCE_TYPES`
    when unset, blank, or all-empty. Values are lowercased to match the
    ``source_type`` column convention.
    """
    raw = os.getenv("LEGBA_GROUNDING_TRUSTED_SOURCE_TYPES")
    if not raw or not raw.strip():
        return _DEFAULT_TRUSTED_SOURCE_TYPES
    parsed = tuple(t.strip().lower() for t in raw.split(",") if t.strip())
    return parsed or _DEFAULT_TRUSTED_SOURCE_TYPES


def contention_surfacing_enabled() -> bool:
    """P3-2 — whether the grounding preamble ANNOTATES a disputed fact
    (CONTESTED/DISPUTED). Default ON (``LEGBA_CONTENTION_SURFACING`` truthy or
    unset); set the flag to a falsey value ('0'/'off'/'false'/'no') for a kill
    switch that renders disputed facts plainly (no annotation). This is a pure
    read annotation — it never changes WHICH fact is grounded, only whether the
    dispute is disclosed to the reading LLM."""
    raw = os.getenv("LEGBA_CONTENTION_SURFACING")
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def contention_prefer_surfaced() -> bool:
    """P3-2 — whether slice assembly PREFERS the arbiter's surfaced winner over
    its contested siblings in the grounding ORDER BY. **Default OFF**
    (``LEGBA_CONTENTION_SURFACING_PREFER`` unset/falsey): this changes WHAT
    analysts consume, so it never ships silently — the annotation (above) stays
    the ON default, the preference is an explicit opt-in."""
    raw = os.getenv("LEGBA_CONTENTION_SURFACING_PREFER")
    if raw is None or not raw.strip():
        return False
    return raw.strip().lower() in ("1", "true", "yes", "on")


# A bare Wikidata QID (``Q22686``) — what a fact value degrades to when an
# upstream label lookup failed. Injecting "head of state: Q22686" is worse than
# injecting nothing (the LLM can't read it), so any value/term that is JUST a
# QID is skipped at the resolver chokepoint — we degrade to no-grounding for
# that fact rather than emit an unreadable line. Conservative by construction:
# the anchored pattern matches ONLY a bare QID; a normal name ("Donald Trump",
# even one that happens to contain a Q) passes straight through.
_BARE_QID_RE = re.compile(r"^Q[0-9]+$")


def _is_bare_qid(value: Any) -> bool:
    """True only when ``value`` is a bare Wikidata QID (``^Q[0-9]+$``)."""
    return isinstance(value, str) and _BARE_QID_RE.match(value.strip()) is not None


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from an asyncpg Record OR a plain dict, tolerating absence.

    asyncpg ``Record`` raises ``KeyError`` on a missing column and has no
    ``.get``, while the in-process test stubs hand back plain dicts that may
    omit the newer joined columns (the Wave-5 contention annotation). This
    keeps both shapes working — a row lacking the column degrades to
    ``default`` instead of raising, so an older row shape is uncontested."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


# ---------------------------------------------------------------------------
# R-1 — standing-question backlog ranking (pure, DB-free — unit-testable)
# ---------------------------------------------------------------------------
#
# The corpus_researcher (and any other backlog-draining analyst that opts into
# the ``open_questions`` grounding source) needs a BOUNDED, DETERMINISTIC
# priority order over the standing question set
# (``hypotheses.status='open_question'``) so the model is handed a real
# choice rather than inventing its own topic. The ordering is intentionally a
# pure function of (live_reach, harvest_class, desk_salience, age, id) — no
# LLM, no randomness, reproducible run-to-run and unit-testable without a DB.

# Harvest-class priority (see scripts/harvest_open_questions.py K-2a + the
# K-2b unit-payload faucet in inline_target.convert_open_questions). Ordinal,
# smaller = higher priority. The ordering reasons about which classes are
# actually answerable by RE-MINING OUR OWN full-text corpus (the tool this
# analyst has) vs. classes that need something else:
#   * below_floor / fact_contention / freshness_advisory / scorecard_
#     disagreement — each is a question about whether EXISTING evidence
#     supports a claim; a deeper corpus read is exactly the right instrument.
#   * unit_payload — a per-finding uncertainty a unit flagged live (K-2b);
#     concrete but not yet vetted by a harvest sweep, so ranks after the
#     four harvested classes.
#   * collection_gap — by DEFINITION a desk×dimension our sources are
#     starved on; re-mining what we already ingested is the LEAST likely of
#     the six to resolve it (that is R-2/R-3's job — collection requirements /
#     external retrieval). Still eligible: a plain "the corpus does not cover
#     this" is itself a legitimate, informative finding — it just ranks last.
_HARVEST_CLASS_PRIORITY: dict[str, int] = {
    "below_floor": 0,
    "fact_contention": 1,
    "freshness_advisory": 2,
    "scorecard_disagreement": 3,
    "unit_payload": 4,
    "collection_gap": 5,
}
# A harvest class this table has never seen (schema drift / a future class) —
# still eligible, ranked after every known class rather than crashing.
_UNKNOWN_HARVEST_CLASS_PRIORITY = 6

# The idempotency-marker key both the K-2a harvest script and the K-2b
# unit-payload converter stamp into ``hypotheses.diagnostic_evidence``.
_OPEN_QUESTION_MARKER_KEY = "open_question_origin"


def _parse_diagnostic_evidence(raw: Any) -> list[Any]:
    """``hypotheses.diagnostic_evidence`` as a list, tolerating asyncpg's
    str-or-native jsonb shape. Malformed/absent -> ``[]`` (never raises)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    return raw if isinstance(raw, list) else []


def harvest_class_of(diagnostic_evidence: Any) -> str:
    """The harvest-class label for one standing question, from its
    ``open_question_origin`` marker (see the module-level K-2a/K-2b note).

    ``origin='harvest'`` carries an explicit ``harvest_class`` (one of
    :data:`_HARVEST_CLASS_PRIORITY`'s five harvested keys); ``origin=
    'unit_payload'`` (the K-2b per-finding faucet) has none, so it is labeled
    directly; anything else — a marker-less row, a future origin, malformed
    jsonb — reads as ``'unknown'`` rather than raising. Pure + DB-free.
    """
    for entry in _parse_diagnostic_evidence(diagnostic_evidence):
        if not isinstance(entry, Mapping):
            continue
        if entry.get("marker") != _OPEN_QUESTION_MARKER_KEY:
            continue
        origin = entry.get("origin")
        if origin == "harvest":
            cls = entry.get("harvest_class")
            return str(cls) if cls else "unknown"
        if origin == "unit_payload":
            return "unit_payload"
    return "unknown"


def open_question_priority_key(
    *,
    live_reach: int,
    harvest_class: str,
    desk_salience: float,
    age_days: float,
    question_id: str,
) -> tuple[int, int, int, float, float, str]:
    """Deterministic sort key for standing-question priority (ASCENDING sort
    == highest priority first). Pure + DB-free — unit-testable directly.

    Tiers, in order:
      1. Traces FORWARD to a live (non-superseded) product
         (``live_reach > 0``, the ``output_consumption`` forward walk) —
         these are the questions actually driving ``staleness_debt``;
         resolving one retires live product debt, not just backlog trivia.
      2. Among those, bigger ``live_reach`` (more live products resting on
         it) first — the highest-leverage question to research.
      3. Harvest-class ordinal (:data:`_HARVEST_CLASS_PRIORITY`) — see that
         table's docstring for the "answerable by re-mining our own corpus"
         reasoning.
      4. Desk salience — the question's target desk's hottest OPEN
         situation intensity (0.0 when the question has no target_id, or its
         desk has no open situation) — a hotter desk's open question is more
         consequential to resolve first.
      5. Question age — OLDER first: the longer a question has stood
         unresolved, the more backlog debt it represents.
      6. Question id — final deterministic tie-break so the ordering is
         100% reproducible (and testable) when every other key ties.
    """
    return (
        0 if live_reach > 0 else 1,
        -live_reach,
        _HARVEST_CLASS_PRIORITY.get(harvest_class, _UNKNOWN_HARVEST_CLASS_PRIORITY),
        -desk_salience,
        -age_days,
        question_id,
    )


# A name shorter than this is too generic to ground on (drops "US", bare
# initials, junk NER 1-2 char tags). 3 keeps real country/person names.
_MIN_CANDIDATE_LEN = 3
# How many distinct candidate names we resolve at most (a guard so a
# tag-heavy slice can't fan out into hundreds of ILIKE probes). The per-fact
# cap (``max_facts``) bounds the OUTPUT; this bounds the QUERY width.
_MAX_CANDIDATES = 24
# How many signed nexuses (alliances/hostility) we fold in alongside facts.
# Small — the structural picture is a few load-bearing edges, not the graph.
_MAX_NEXUSES = 12
# How many ongoing situation frames we fold into the (separate, clearly-
# labelled) ASSESSED SITUATIONS block — the most intense open frames, not the
# whole list.
_MAX_SITUATIONS = 8
# How many items per category (tense actors / brokers / proxy chains) we fold
# into the (separate, clearly-labelled) ASSESSED STRUCTURE block — the headline
# interesting structures the knowledge graph surfaced, not the full enumeration.
_MAX_GRAPH_STRUCTURE = 6
# How many reified narratives (mig 0102 — contested-claim families the
# narrative_mapper derived from the contention sidecar + carrier lineage) we
# fold into the (separate, clearly-labelled) ASSESSED NARRATIVES block. Small:
# the block is a contested-claims heads-up for the narrative_coordination unit,
# not a narrative dump.
_MAX_NARRATIVES = 8
# Per-country narrative scoping over-fetch: subject_key scope-matching runs in
# Python (post-fetch, whole-word against the target's geo names), so the SQL
# fetch takes headroom the way the situations non-event filter does.
_NARRATIVES_SCOPED_FETCH = 64
# R-1 (the corpus_researcher backlog source) — how many STANDING open
# questions we fold into the (separate, clearly-labelled) STANDING OPEN
# QUESTIONS block. Small + fixed regardless of the descriptor's ``max_facts``
# (a backlog-draining researcher answers ONE question per tick; offering it
# hundreds would blow the token budget for no benefit) — see
# ``resolve_open_questions``.
_MAX_OPEN_QUESTIONS_GROUNDING = 8
# How many OPEN (``status='open_question'``) hypotheses rows we fetch as
# ranking candidates before truncating to _MAX_OPEN_QUESTIONS_GROUNDING. Set
# comfortably above the live backlog size (hundreds, per the K-2a harvest) so
# the SAFETY cap never silently excludes a genuinely older/higher-priority
# question from consideration; it exists only to bound a runaway backlog.
_OPEN_QUESTION_CANDIDATE_FETCH_CAP = 1000
# Forward-consumption walk depth for the "does this question trace to a LIVE
# product" signal (mirrors claim_watch.FORWARD_WALK_MAX_DEPTH — the same
# bounded-BFS discipline over output_consumption, migration 0106).
_OPEN_QUESTION_FORWARD_WALK_MAX_DEPTH = 6
# Per-question thesis text cap in the rendered block line (token budget).
_OPEN_QUESTION_THESIS_CHAR_CAP = 400
# Opportunistic RAG (S5-T3) — how many retrieved ``world_context`` chunks we fold
# into the (separate, clearly-labelled) BACKGROUND PRIORS block. Small: this is a
# few framing priors, NOT an evidence dump; it rides alongside the token-heavy
# ground-truth + situations + structure blocks, so the ceiling is tight.
_MAX_WORLD_CONTEXT_CHUNKS = 2  # DQ Phase-2 RAG tune (2026-07-03): 6->2 — fewer priors = less uncited-interpretation leak surface + lower token cost (C1/RAG mechanism finding).
# Per-chunk character cap — a single retrieved chunk is trimmed to this before it
# reaches the prompt (the Lane-4 chunker targets ~400-800 tokens, but a stray
# long chunk must not blow the context). Token-cap via chars, degrade-not-drop.
_WORLD_CONTEXT_CHUNK_CHAR_CAP = 700
# Total BACKGROUND PRIORS block character cap — the render stops folding chunks
# once the accumulated block would exceed this, so the priors can never crowd out
# the authoritative context + the signal slice.
_WORLD_CONTEXT_BLOCK_CHAR_CAP = 3000
# Relevance floor for the opportunistic ``world_context`` RAG (S5-T3). A retrieved
# chunk must clear this cosine-similarity score to be injected as a prior; a chunk
# below it is DROPPED and, when ALL retrieved chunks fall below, NO block is built
# (degrade-not-drop — ``build_world_context_block([])`` returns ``None``).
#
# CALIBRATION (M22, 2026-07-06 — live embedding probe against the 293-point
# world_context corpus, the `embedding-inno1` / bge-m3 endpoint). The 0.65 floor
# filtered ~100% of hits (0/81 runs injected) because it sat ABOVE the achievable
# on-target cosine for this embedder+corpus. Two paired retrieval fixes recalibrate
# the distribution (query construction + doc-side context prefix; see
# analyst_deps_builder._world_context_query and lane4_loader.contextual_embedding_input):
#   * on-target (target-country background chunks): best chunk ~0.60-0.66
#   * off-target (another country's chunks):        ~0.40-0.43
# So a floor in [0.50, 0.56] discriminates cleanly (wide margin either side); we
# pick the HIGH end for PRECISION (the RAG rollback showed marginal hits fuel
# uncited-interpretation leak) — 0.55 admits only genuinely-strong on-target priors
# and rejects every off-target chunk by ~0.13. NOTE: this margin was measured
# CROSS-COUNTRY (on-target country's chunks vs another country's) — the case the
# per-desk Qdrant MatchAny country filter already scopes; the harder INTRA-country
# SECTION discrimination (does the RIGHT section outrank a weaker same-country one?)
# is not validated by the probe and will be measured live by the #179 instrumentation
# (world_context_top_score / retained, now on every ground trace). Raise toward 0.58-0.60 AFTER the
# corpus re-embed (scripts/reembed_world_context.py) lifts every desk's top chunk
# over 0.60. Applied server-side via Qdrant's ``score_threshold`` AND re-checked
# client-side in :meth:`SubstrateGroundingResolver._map_world_context_hits` (a
# client / stub that ignores the threshold still can't leak a below-floor chunk).
# Env-overridable via ``LEGBA_WORLD_CONTEXT_MIN_SCORE``; a bad / blank value falls
# back to default.
_WORLD_CONTEXT_MIN_SCORE = 0.55


def world_context_min_score() -> float:
    """The minimum cosine similarity a ``world_context`` chunk needs to ground.

    Reads ``LEGBA_WORLD_CONTEXT_MIN_SCORE`` (a float); falls back to
    :data:`_WORLD_CONTEXT_MIN_SCORE` when unset, blank, or malformed. Never
    raises — grounding is an enrichment, so a bad env value degrades to default.
    """
    raw = os.getenv("LEGBA_WORLD_CONTEXT_MIN_SCORE")
    if not raw or not raw.strip():
        return _WORLD_CONTEXT_MIN_SCORE
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return _WORLD_CONTEXT_MIN_SCORE


# Quality guard for the situations grounding block (review M2 + DQ P6). Clustered
# "nothing to report" / status-quo findings get a signature + materialise as
# situations; they are NON-events and must never be injected as an ongoing
# situation NOR head the intensity ranking. Two shape families match:
#
#   1. LEGACY "No … (alerts / -specific / in the latest batch)" — the extinct
#      weather-alert non-event ("No France-specific weather alerts in the latest
#      batch of signals").
#   2. DQ P6 MID-STRING status-quo shapes — the live pollution the legacy anchor
#      missed: "United States – No observable WMD proliferation activity",
#      "Canada – Stability maintained (no dominant instability vector)",
#      "North Korea – Status quo across examined domains", "Russia – Low
#      leadership transition risk". The single highest-intensity open frame
#      platform-wide was one of these (a fold-count-driven non-event heading the
#      global grounding block), so mid-string negation/status-quo must match too.
#
# STILL CONSERVATIVE — the "No <qualifier>" branch is DOUBLY anchored so it fires
# only on a desk-name status-quo shape, never on a real event that merely mentions
# a negated qualifier mid-sentence:
#   (1) LEAD-IN anchor: the "No" must sit at a name-segment boundary — string start
#       or right after a desk separator (–/—/-/:/'(') — so "Airstrikes continue with
#       no significant activity …" (a mid-sentence "no significant") does NOT match.
#   (2) TRAILING status-noun anchor: within the SAME segment (stopped at '.'/';') the
#       negated qualifier must be followed by a static observation noun
#       (activity/shift/posture/pressure/signals/narrative/…), so the real event
#       "No significant de-escalation; airstrikes intensify along the border" — whose
#       only post-qualifier word before the ';' is the CHANGE noun "de-escalation" —
#       does NOT match, while "No observable WMD proliferation activity" (ends in the
#       state noun "activity") and "No clear standing military posture shift" (Japan/
#       Saudi live frames — 'clear'/'evident' are non-observation qualifiers too) DO.
# "No-fly zone declared …" (no space before "-fly") and "No deal reached …" ("deal"
# is not a qualifier) still fall through. The "low … risk" branch is scoped to the
# near-term/multi-domain/overall/leadership-transition risk-level qualifiers, so
# "Drives Escalation Risk" (no leading "low") stays a real frame; legitimate
# energy_security "low/elevated energy-security pressure" reads (no "risk") are
# deliberately NOT matched (a low-pressure read is a real assessment).
#
# LOCKSTEP: migration 0071 carries the POSIX (`~*`) mirror of this exact pattern —
# change both together (tests assert each branch on the shared live shapes).
_NON_EVENT_SITUATION_RE = re.compile(
    r"(?:^\s*no\b.*?(?:in the latest batch|[- ]specific|alerts?))"
    r"|(?:(?:^|[–—:(-])\s*no\s+(?:dominant|observable|discernible|significant|"
    r"coordinated|credible|material|notable|meaningful|apparent|clear|evident)\b"
    r"[^.;]*?\b(?:activit(?:y|ies)|shifts?|posture|pressure|signals?|"
    r"narratives?|detected|observed|vectors?|instabilit(?:y|ies)|movements?|"
    r"buildups?|mobilization|maneuvers?|indications?|deployments?|incidents?|"
    r"unrest|anomal(?:y|ies))\b)"
    r"|(?:\bstatus\s+quo\b)"
    r"|(?:\bstability\s+maintained\b)"
    r"|(?:\blow\s+(?:near[-\s]?term|multi[-\s]?domain|overall|"
    r"leadership\s+transition)\b[^.]{0,24}\brisk\b)",
    re.IGNORECASE,
)


def situation_grounding_min_intensity() -> float:
    """Minimum ``intensity_score`` an ongoing frame needs to ground.

    Reads ``LEGBA_SITUATION_GROUNDING_MIN_INTENSITY``; defaults to 0.0 (off) so
    the gate is opt-in. A bad value falls back to 0.0.
    """
    raw = os.getenv("LEGBA_SITUATION_GROUNDING_MIN_INTENSITY")
    if not raw or not raw.strip():
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


def is_non_event_situation_name(name: Any) -> bool:
    """True for a clustered 'nothing to report' / status-quo non-event frame.

    Shared, dependency-light predicate: the grounding READ uses it to drop these
    frames from the injected block, and :mod:`situation_clustering` reuses it at
    MATERIALIZATION time (DQ P6) to stamp ``data.steady_state`` so a steady-state
    frame is authoritatively marked at write time, not only name-filtered on read.
    Keep the two call sites on the SAME regex so the tag and the read never drift.
    """
    return isinstance(name, str) and _NON_EVENT_SITUATION_RE.search(name) is not None


# Back-compat private alias (existing call sites in this module).
_is_non_event_situation = is_non_event_situation_name


class GroundingFact:
    """A single current authoritative fact row, normalised for rendering.

    CONTESTED annotation (Holes-B Wave 5 — #101). A grounding-eligible
    (seed/curated) fact can still belong to a live contention group when the
    operator vetted two coexisting values for the same ``(subject, predicate)``
    or an ingestion-side dispute names a curated value as one of its clusters.
    The thin ``facts`` markers (``contested`` / ``surfaced_winner``) joined
    against ``fact_contention`` let the preamble TELL the reading LLM that a
    value is disputed instead of asserting a disputed value as plain ground
    truth. Three states the renderer distinguishes:

      * ``contested=False`` — the common case; rendered exactly as before.
      * ``contested=True`` AND ``surfaced_winner=True`` — the arbiter picked
        THIS value as the (current, deterministic) winner over N-1 others.
        Rendered with "(CONTESTED: N sources disagree; surfaced winner)".
      * ``contested=True`` AND ``surfaced_winner=False`` — a contested
        non-winner, or a group the arbiter ABSTAINED on (no surfaced winner).
        Rendered "DISPUTED" so it is NEVER read as settled fact.

    ``contention_value_count`` is the group's distinct NON-junk value-cluster
    count (the "N sources disagree" N — really N competing values); NULL/absent
    when uncontested. The annotation NEVER injects ingestion content: only the
    already-eligible fact's own line is decorated; the sibling values live in
    the sidecar (surfaced by the read API), not in the ground-truth preamble.

    DECAY annotation (C4, flag ``LEGBA_FACT_DECAY_WEIGHTING`` — default OFF):
    when the flag is ON, ``_query_facts`` also joins the ``fact_decay_states``
    sidecar (migration 0098) and threads ``decayed_confidence`` /
    ``decay_state`` here; an ``aging``/``stale`` fact's line gets an explicit
    age annotation (``revoke_candidate`` rows never reach the preamble — the
    SQL excludes them). Flag OFF (the shipped default): both stay ``None``
    and the rendered line is byte-identical to the pre-C4 output.
    """

    __slots__ = (
        "subject",
        "predicate",
        "value",
        "valid_from",
        "source_type",
        "confidence",
        "contested",
        "surfaced_winner",
        "contention_value_count",
        "decayed_confidence",
        "decay_state",
    )

    def __init__(
        self,
        *,
        subject: str,
        predicate: str,
        value: str,
        valid_from: datetime | None,
        source_type: str | None,
        confidence: float | None,
        contested: bool = False,
        surfaced_winner: bool = False,
        contention_value_count: int | None = None,
        decayed_confidence: float | None = None,
        decay_state: str | None = None,
    ) -> None:
        self.subject = subject
        self.predicate = predicate
        self.value = value
        self.valid_from = valid_from
        self.source_type = source_type
        self.confidence = confidence
        self.contested = bool(contested)
        self.surfaced_winner = bool(surfaced_winner)
        self.contention_value_count = contention_value_count
        self.decayed_confidence = decayed_confidence
        self.decay_state = decay_state

    def render(self) -> str:
        since = ""
        if isinstance(self.valid_from, datetime):
            since = f" (since {self.valid_from.date().isoformat()})"
        return (
            f"{self.subject} — {self.predicate}: {self.value}{since}"
            f"{self._contested_suffix()}{self._decay_suffix()}"
        )

    def _decay_suffix(self) -> str:
        """The decay annotation appended under LEGBA_FACT_DECAY_WEIGHTING.

        Empty when no decay readout was threaded (flag OFF — the shipped
        default — or no sidecar row yet) and for ``fresh`` facts, so the
        common case renders byte-identically to the pre-C4 line. An
        aging/stale fact is annotated so the LLM weighs an unre-observed
        assertion below a recently-sighted one."""
        if self.decay_state not in ("aging", "stale"):
            return ""
        dc = ""
        if self.decayed_confidence is not None:
            dc = f"; decayed confidence {float(self.decayed_confidence):.2f}"
        return f"  [{self.decay_state.upper()}: not recently re-observed{dc}]"

    def _contested_suffix(self) -> str:
        """The CONTESTED/DISPUTED annotation appended to a contested fact line.

        Empty for the uncontested common case. A surfaced winner reads as the
        current best answer but flags the disagreement + value count; a
        contested non-winner / abstained group reads "DISPUTED" so the LLM
        never treats the value as settled ground truth."""
        if not self.contested:
            return ""
        n = self.contention_value_count
        n_str = str(n) if isinstance(n, int) and n > 0 else "multiple"
        if self.surfaced_winner:
            return f"  [CONTESTED: {n_str} sources disagree; surfaced winner]"
        return f"  [DISPUTED: {n_str} sources disagree; no surfaced winner — do not treat as settled]"


class GroundingNexus:
    """A single current signed relationship (alliance/hostility), for rendering."""

    __slots__ = ("subject", "rel_type", "object", "polarity", "valid_from")

    def __init__(
        self,
        *,
        subject: str,
        rel_type: str,
        object: str,
        polarity: int | None,
        valid_from: datetime | None,
    ) -> None:
        self.subject = subject
        self.rel_type = rel_type
        self.object = object
        self.polarity = polarity
        self.valid_from = valid_from

    def render(self) -> str:
        sign = ""
        if self.polarity is not None and self.polarity < 0:
            sign = " [antagonistic]"
        elif self.polarity is not None and self.polarity > 0:
            sign = " [supportive]"
        since = ""
        if isinstance(self.valid_from, datetime):
            since = f" (since {self.valid_from.date().isoformat()})"
        return f"{self.subject} {self.rel_type} {self.object}{sign}{since}"


class GroundingSituation:
    """An ONGOING situation frame, for the (separate, clearly-labelled) ASSESSED
    SITUATIONS block — analysis-derived persistent context, NOT ground truth."""

    __slots__ = ("name", "category", "status", "intensity_score", "valid_from", "last_event_at")

    def __init__(
        self,
        *,
        name: str,
        category: str | None,
        status: str | None,
        intensity_score: float | None,
        valid_from: datetime | None,
        last_event_at: datetime | None = None,
    ) -> None:
        self.name = name
        self.category = category
        self.status = status
        self.intensity_score = intensity_score
        self.valid_from = valid_from
        self.last_event_at = last_event_at

    def render(self, *, now: datetime | None = None) -> str:
        bits: list[str] = []
        if self.status:
            bits.append(str(self.status))
        if self.intensity_score is not None:
            bits.append(f"intensity {self.intensity_score:.1f}")
        if isinstance(self.valid_from, datetime):
            bits.append(f"ongoing since {self.valid_from.date().isoformat()}")
        # Staleness signal (review follow-up): a dormant frame's last member
        # finding can be days old — surface the age so the reading LLM can
        # down-weight a quiet frame rather than treating it as live news.
        if isinstance(self.last_event_at, datetime):
            ref = now or datetime.now(tz=timezone.utc)
            age_days = max(0, (ref - self.last_event_at).days)
            bits.append(
                "last activity today" if age_days == 0
                else f"last activity {age_days}d ago"
            )
        meta = f" [{'; '.join(bits)}]" if bits else ""
        return f"{self.name}{meta}"


class GroundingNarrative:
    """One reified NARRATIVE (a contested-claim family, mig 0102) for the
    (separate, clearly-labelled) ASSESSED NARRATIVES block — detect-only,
    analysis-derived propagation context, NOT ground truth. Mirrors the
    narrative_mapper's honesty contract: echo/lead ordering is DESCRIPTIVE
    publish-order timing, never a coordination verdict."""

    __slots__ = (
        "subject_key", "predicate_key", "status", "surfaced_value",
        "variant_count", "carrier_source_count", "publish_dated_source_count",
        "first_seen_at", "last_seen_at", "lead_source_id", "max_echo_lag_hours",
    )

    def __init__(
        self,
        *,
        subject_key: str,
        predicate_key: str,
        status: str | None,
        surfaced_value: str | None,
        variant_count: int,
        carrier_source_count: int,
        publish_dated_source_count: int = 0,
        first_seen_at: datetime | None = None,
        last_seen_at: datetime | None = None,
        lead_source_id: str | None = None,
        max_echo_lag_hours: float | None = None,
    ) -> None:
        self.subject_key = subject_key
        self.predicate_key = predicate_key
        self.status = status
        self.surfaced_value = surfaced_value
        self.variant_count = variant_count
        self.carrier_source_count = carrier_source_count
        self.publish_dated_source_count = publish_dated_source_count
        self.first_seen_at = first_seen_at
        self.last_seen_at = last_seen_at
        self.lead_source_id = lead_source_id
        self.max_echo_lag_hours = max_echo_lag_hours

    def render(self) -> str:
        head = (
            f"[{self.status or 'contested'}] "
            f"'{self.subject_key}' {self.predicate_key}: "
            f"{self.variant_count} competing variant(s) across "
            f"{self.carrier_source_count} source(s)"
        )
        bits: list[str] = []
        if isinstance(self.first_seen_at, datetime) and isinstance(
            self.last_seen_at, datetime
        ):
            bits.append(
                f"active {self.first_seen_at.date().isoformat()} -> "
                f"{self.last_seen_at.date().isoformat()}"
            )
        elif isinstance(self.last_seen_at, datetime):
            bits.append(f"last seen {self.last_seen_at.date().isoformat()}")
        if self.lead_source_id:
            lead = f"first published by {self.lead_source_id}"
            if self.max_echo_lag_hours is not None:
                lead += (
                    f", echoed up to {self.max_echo_lag_hours:.0f}h later "
                    "(publish-order timing only — NOT evidence of copying)"
                )
            bits.append(lead)
        if self.surfaced_value:
            bits.append(f"arbiter-surfaced winner='{self.surfaced_value}'")
        else:
            bits.append("no surfaced winner — do not treat any variant as settled")
        meta = f" [{'; '.join(bits)}]" if bits else ""
        return f"{head}{meta}"


class GroundingOpenQuestion:
    """One STANDING open question (``hypotheses.status='open_question'``) for
    the (separate, clearly-labelled) STANDING OPEN QUESTIONS block (R-1) — the
    corpus_researcher's backlog source. Analysis-derived (harvested via K-2a
    or unit-flagged via the K-2b per-finding faucet), NEVER laundered into the
    ground-truth block: the analyst is told to PREFER answering one of these,
    but a null result (the corpus does not resolve it) is an equally
    legitimate finding — this never asserts the question IS answerable."""

    __slots__ = (
        "id", "thesis", "harvest_class", "target_id", "produced_at",
        "live_reach", "desk_salience",
    )

    def __init__(
        self,
        *,
        id: Any,
        thesis: str,
        harvest_class: str,
        target_id: str | None,
        produced_at: datetime | None,
        live_reach: int,
        desk_salience: float,
    ) -> None:
        self.id = id
        self.thesis = thesis
        self.harvest_class = harvest_class
        self.target_id = target_id
        self.produced_at = produced_at
        self.live_reach = live_reach
        self.desk_salience = desk_salience

    def render(self, *, tag: str, now: datetime | None = None) -> str:
        ref = now or datetime.now(timezone.utc)
        bits: list[str] = [self.harvest_class]
        if isinstance(self.produced_at, datetime):
            p = (
                self.produced_at if self.produced_at.tzinfo
                else self.produced_at.replace(tzinfo=timezone.utc)
            )
            age_days = max(0, (ref - p).days)
            bits.append("opened today" if age_days == 0 else f"opened {age_days}d ago")
        if self.live_reach > 0:
            bits.append(f"live_reach={self.live_reach}")
        thesis = (self.thesis or "").strip()
        if len(thesis) > _OPEN_QUESTION_THESIS_CHAR_CAP:
            thesis = thesis[:_OPEN_QUESTION_THESIS_CHAR_CAP].rstrip() + "…"
        meta = f" ({'; '.join(bits)})" if bits else ""
        return f"[{tag}]{meta} {thesis}"


class GroundingInterestingItem:
    """One ranked entry from the graph_metrics ``interesting`` shortlist (the
    shared contract that ``graph_mining`` + ``structural_balance`` now ADD to
    their payload). Preferred over the raw frustration/betweenness enumeration
    when present — it carries the producer's own rationale + ranking, so the
    block reads as a curated shortlist rather than a metric dump.

    Fields mirror the contract: ``kind`` (tense_actor | broker | new_hostile_edge
    | sign_imbalanced_triad | proxy_chain), ``label`` (human label), ``score``
    (0..1, higher = more interesting), ``rationale`` (one NL line), ``entities``
    (the entity names involved, for scope matching)."""

    __slots__ = ("kind", "label", "score", "rationale", "entities")

    def __init__(
        self,
        *,
        kind: str,
        label: str,
        score: float,
        rationale: str,
        entities: list[str],
    ) -> None:
        self.kind = kind
        self.label = label
        self.score = score
        self.rationale = rationale
        self.entities = entities


class GroundingGraphStructure:
    """The headline interesting STRUCTURES the knowledge graph surfaced, for the
    (separate, clearly-labelled) ASSESSED STRUCTURE block — analysis-derived from
    the signed relationship graph (structural_balance + graph_mining metrics),
    NOT ground truth.

    PREFERRED path — ``interesting``: the ranked shortlist the producers now emit
    on their graph_metrics payload (the shared contract). When present, the block
    renders these (kind-grouped, with each item's rationale) and the raw
    enumeration below is left empty.

    FALLBACK path — the raw enumeration extracted from the metric maps when no
    ``interesting`` list is present (older payloads):

    * ``frustration`` — (actor, sign-imbalanced-triad count): the most
      structurally TENSE actors (caught in conflicting/unbalanced ties).
    * ``brokers`` — (actor, betweenness): high-betweenness cut-points that sit
      between otherwise-separated camps.
    * ``proxy_chains`` — pre-rendered indirect/cut-out link strings (A → via → B).
    """

    __slots__ = ("frustration", "brokers", "proxy_chains", "interesting")

    def __init__(
        self,
        *,
        frustration: list[tuple[str, float]],
        brokers: list[tuple[str, float]],
        proxy_chains: list[str],
        interesting: list[GroundingInterestingItem] | None = None,
    ) -> None:
        self.frustration = frustration
        self.brokers = brokers
        self.proxy_chains = proxy_chains
        self.interesting = interesting or []

    def is_empty(self) -> bool:
        return not (self.interesting or self.frustration or self.brokers or self.proxy_chains)


class GroundingWorldContextChunk:
    """One retrieved ``world_context`` RAG chunk, normalised for rendering.

    Opportunistic RAG (S5-T3): a semantic hit from the curated ``world_context``
    Qdrant collection (Lane-4 corpus — country/topic priors, doctrine summaries).
    This is PRIOR, not EVIDENCE — it frames HOW the unit reasons (method, what to
    look for), never WHAT is true. It is rendered in a SEPARATE, clearly-labelled
    "BACKGROUND PRIORS (context, not evidence — do not cite)" block and is NEVER
    citable via ``[N]`` (verify semantics are untouched by construction — the
    block carries no signal ids for the citation index to bind).

    ``chunk_id`` is the Qdrant point id (a deterministic uuid5 of the chunk
    natural key — see :func:`legba.data.rag.lane4_loader._point_id`); it is
    recorded into the analyst trace so every run's retrieved context is auditable
    provenance. ``text`` is the chunk body (``payload['text']``); ``title`` /
    ``section`` label it; ``source_url`` carries attribution when present.
    """

    __slots__ = ("chunk_id", "title", "section", "source_url", "text", "score")

    def __init__(
        self,
        *,
        chunk_id: str,
        title: str | None,
        section: str | None,
        source_url: str | None,
        text: str,
        score: float | None,
    ) -> None:
        self.chunk_id = chunk_id
        self.title = title
        self.section = section
        self.source_url = source_url
        self.text = text
        self.score = score

    def render(self, *, char_cap: int = _WORLD_CONTEXT_CHUNK_CHAR_CAP) -> str:
        """One compact block per chunk: a label line + the (trimmed) body.

        The label prefers ``title — section`` and falls back to whatever is
        present; the body is trimmed to ``char_cap`` (token control) with an
        ellipsis so a stray long chunk can't blow the context.
        """
        label_bits = [b for b in (self.title, self.section) if b and b.strip()]
        label = " — ".join(label_bits) if label_bits else "untitled prior"
        body = (self.text or "").strip()
        if char_cap > 0 and len(body) > char_cap:
            body = body[:char_cap].rstrip() + "…"
        return f"- {label}: {body}" if body else f"- {label}"


def _json_or_dict(payload: Any) -> dict[str, Any]:
    """asyncpg returns a jsonb column as a str (default codec) or a dict — coerce
    to a dict, degrading a malformed payload to ``{}`` (never raise on grounding)."""
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _top_graph_items(
    d: Any, cand_lc: set[str], limit: int, *, min_value: float | None = None,
    target_scoped: bool = False,
) -> list[tuple[str, float]]:
    """From a {name: numeric} map, the top-``limit`` (name, value) pairs — names
    matching the assessor's candidate entities FIRST (their structure is the most
    relevant), then the highest-value global structures. Junk names (too short /
    bare QID) are dropped.

    ``target_scoped`` (D4 contamination fix): when True AND a candidate set is
    present, the global out-of-scope tail is DROPPED entirely rather than used to
    top up the limit. This is the per-country path — a country_assessor must NOT
    inherit the globally-most-central node (the US is the most-central node in the
    signed graph, so the global top-up made every country's ASSESSED STRUCTURE
    block US-centric). A no-target / meta run (world_assessor) leaves this False,
    so the global structure block is preserved unchanged (the global picture is
    correct for the global assessor)."""
    if not isinstance(d, dict):
        return []
    items: list[tuple[str, float]] = []
    for name, val in d.items():
        if not isinstance(name, str) or len(name.strip()) < _MIN_CANDIDATE_LEN:
            continue
        if _is_bare_qid(name):
            continue
        try:
            v = float(val)
        except (TypeError, ValueError):
            continue
        if min_value is not None and v <= min_value:
            continue
        items.append((name, v))
    in_scope = sorted(
        (it for it in items if it[0].casefold() in cand_lc), key=lambda x: x[1], reverse=True,
    )
    # PER-COUNTRY: drop the global tail outright (no US-centric top-up). Only when
    # there actually IS a candidate scope — a scoped run with an empty candidate
    # set degrades to the global view rather than emitting nothing.
    if target_scoped and cand_lc:
        return in_scope[:limit]
    out_scope = sorted(
        (it for it in items if it[0].casefold() not in cand_lc), key=lambda x: x[1], reverse=True,
    )
    return (in_scope + out_scope)[:limit]


def _top_brokers(
    d: Any, cand_lc: set[str], limit: int, *, target_scoped: bool = False,
) -> list[tuple[str, float]]:
    """Flatten {name: {betweenness, degree}} → top-``limit`` (name, betweenness),
    keeping only true brokers (betweenness > 0 — a high-degree hub with zero
    betweenness, e.g. a catalog body, is NOT a cut-point).

    ``target_scoped`` is forwarded to :func:`_top_graph_items` (per-country drops
    the global broker tail; meta keeps it)."""
    if not isinstance(d, dict):
        return []
    flat: dict[str, float] = {}
    for name, metrics in d.items():
        b = metrics.get("betweenness") if isinstance(metrics, dict) else None
        try:
            bf = float(b)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if bf > 0:
            flat[name] = bf
    return _top_graph_items(
        flat, cand_lc, limit, min_value=0.0, target_scoped=target_scoped,
    )


def _render_proxy_chain(c: Any) -> str | None:
    """Best-effort one-line render of a discovered proxy/cut-out chain (the
    graph_mining payload shape varies across versions — handle str/dict/list)."""
    if isinstance(c, str):
        return c.strip() or None
    if isinstance(c, dict):
        subj = c.get("subject") or c.get("source") or c.get("a") or c.get("head")
        via = c.get("intermediary") or c.get("via") or c.get("through")
        obj = c.get("object") or c.get("target") or c.get("b") or c.get("tail")
        sign = c.get("sign") or c.get("polarity") or c.get("sign_product")
        if subj and obj:
            mid = f" → {via} →" if via else " →"
            tag = ""
            try:
                if sign is not None and float(sign) < 0:
                    tag = " [hostile path]"
                elif sign is not None and float(sign) > 0:
                    tag = " [aligned path]"
            except (TypeError, ValueError):
                pass
            return f"{subj}{mid} {obj}{tag}"
    if isinstance(c, (list, tuple)) and len(c) >= 2:
        return " → ".join(str(x) for x in c)
    return None


def _top_proxy_chains(
    chains: Any, cand_lc: set[str], limit: int, *, target_scoped: bool = False,
) -> list[str]:
    """Render up to ``limit`` notable proxy chains, preferring ones that touch a
    candidate entity.

    ``target_scoped`` (D4): when True AND a candidate scope is present, chains
    that touch NO candidate entity are dropped — a per-country block must not
    inherit a global proxy chain between two other countries. Meta keeps them."""
    if not isinstance(chains, list):
        return []
    rendered = [(s, any(n in s.casefold() for n in cand_lc))
                for s in (_render_proxy_chain(c) for c in chains) if s]
    if target_scoped and cand_lc:
        ordered = [s for s, hit in rendered if hit]
    else:
        # candidate-touching chains first, order otherwise preserved (already ranked upstream)
        ordered = [s for s, hit in rendered if hit] + [s for s, hit in rendered if not hit]
    out: list[str] = []
    for s in ordered:
        if s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


# The kinds the shared ``interesting`` shortlist contract emits. An item with an
# unknown kind still renders (under its own kind label) — the set is only used to
# fix a stable group ORDER for the rendered block.
_INTERESTING_KIND_ORDER: tuple[str, ...] = (
    "tense_actor",
    "broker",
    "new_hostile_edge",
    "sign_imbalanced_triad",
    "proxy_chain",
)


def _collect_interesting(
    payloads: Mapping[str, dict[str, Any]], cand_lc: set[str], limit: int,
    *, target_scoped: bool = False,
) -> list[GroundingInterestingItem]:
    """Merge + rank the ``interesting`` shortlists across the metric payloads
    (the shared contract). Items touching a candidate entity rank FIRST (their
    structure is the most relevant to this assessor's scope), then by descending
    ``score``. Junk rows (no label, non-numeric score) are dropped; the merged
    list is de-duplicated on (kind, label) and capped at ``limit``.

    ``limit`` here bounds the WHOLE shortlist (not per-kind) — the producers
    already cap at ~12 and self-rank, so a single overall cap keeps the block
    tight without re-imposing the per-category fallback shape.

    ``target_scoped`` (D4 contamination fix): when True AND a candidate scope is
    present, out-of-scope items are DROPPED entirely (not merely out-ranked) —
    this is the per-country path, so a country_assessor's ASSESSED STRUCTURE
    block contains ONLY structures touching its own entities and never inherits
    the globally-most-central (US-centric) shortlist. A no-target / meta run
    leaves this False, so the merged global shortlist is preserved unchanged.
    """
    merged: list[tuple[GroundingInterestingItem, bool]] = []
    seen: set[tuple[str, str]] = set()
    for payload in payloads.values():
        raw = payload.get("interesting")
        if not isinstance(raw, list):
            continue
        for it in raw:
            if not isinstance(it, Mapping):
                continue
            label = it.get("label")
            if not isinstance(label, str) or not label.strip():
                continue
            kind = it.get("kind")
            kind = kind.strip() if isinstance(kind, str) and kind.strip() else "structure"
            try:
                score = float(it.get("score"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                score = 0.0
            key = (kind, label.strip().casefold())
            if key in seen:
                continue
            seen.add(key)
            entities = [
                str(e) for e in (it.get("entities") or []) if isinstance(e, str) and e.strip()
            ]
            rationale = it.get("rationale")
            rationale = rationale.strip() if isinstance(rationale, str) else ""
            in_scope = any(e.casefold() in cand_lc for e in entities) or (
                label.strip().casefold() in cand_lc
            )
            merged.append(
                (
                    GroundingInterestingItem(
                        kind=kind,
                        label=label.strip(),
                        score=score,
                        rationale=rationale,
                        entities=entities,
                    ),
                    in_scope,
                )
            )
    # PER-COUNTRY: keep ONLY in-scope items (drop the global tail). Only when a
    # candidate scope actually exists — a scoped run with no candidates degrades
    # to the global view rather than emitting an empty block.
    if target_scoped and cand_lc:
        merged = [t for t in merged if t[1]]
    # candidate-touching first, then score desc (the producers' own ranking).
    merged.sort(key=lambda t: (t[1], t[0].score), reverse=True)
    return [item for item, _ in merged[:limit]]


def situation_scope_for_target(target_id: str | None) -> str | None:
    """The ``situations.target_id`` to scope situation grounding to, or ``None``
    for a GLOBAL view.

    A per-country assessor grounds against ITS country's open frames — situation
    rows whose ``target_id`` equals the assessor's country target (populated at
    clustering time from the finding topic; migration 0042). A global
    meta-analyst (``world_assessor`` — no country target) grounds against the
    most intense open frames across all targets, so it gets ``None``. Scoping on
    the populated ``target_id`` (rather than the ``category==slug`` coincidence)
    means a future THEMATIC situation — different ``target_id`` — never leaks
    into a country assessor's grounding.
    """
    if not target_id or not isinstance(target_id, str):
        return None
    tid = target_id.strip()
    return tid if tid.startswith("country") else None


def is_per_country_target(target_id: str | None) -> bool:
    """True for a per-country run (a ``country_*`` target id) — the path whose
    grounding graph/situation candidates must NARROW to the target geo (D4).

    A meta / no-target run (``world_assessor`` — ``target_id`` None) and a
    non-country thematic target return False, so their GLOBAL structure block is
    left untouched. Mirrors the conservative ``startswith('country')`` test used
    by :func:`situation_scope_for_target` so the two scope decisions never
    diverge."""
    return situation_scope_for_target(target_id) is not None


def world_context_country_filter_values(target_id: str | None) -> list[str] | None:
    """The Qdrant ``MatchAny`` payload-filter values for the ``world_context``
    ``countries`` field, or ``None`` for NO country filter (S5-T3 country guard).

    A single-country desk (``country_<tier>_<iso2>`` — e.g. ``country_g20_us`` /
    ``country_watch_ir``) restricts retrieval to chunks tagged for its OWN
    country: the desk's ISO-2 in the lowercase + UPPER forms the Lane-4 payload
    tags (``payload.countries`` = ``[lower-iso2, UPPER-iso2, CountryName]``), so a
    ``MatchAny`` matches whichever case the loader stored. This is what stops a
    France run from retrieving Iran chunks.

    Returns ``None`` (→ NO filter; the whole collection is eligible) — NEVER an
    empty filter that would match nothing — for the legitimate global cases:

      * a meta / no-target run (``world_assessor`` — ``target_id`` None): the
        global picture is legitimate;
      * a target that doesn't resolve to a single ISO-2 (``region_*`` composers,
        thematic dyads): there is no single country to scope to.

    Never raises — any resolution failure degrades to ``None`` (no filter),
    consistent with the module's degrade-not-drop contract.
    """
    if not is_per_country_target(target_id):
        return None
    tokens = list(_target_id_geo_names(target_id))
    if not tokens:
        return None
    iso2 = tokens[0].strip().lower()
    # Only a bare 2-letter ISO-2 code is a single-country scope; a longer trailing
    # token (or a non-alphabetic one) is not a country desk → no filter (degrade).
    if len(iso2) != 2 or not iso2.isalpha():
        return None
    # Both cases: the Lane-4 payload tags a chunk with BOTH the lower- and
    # UPPER-case ISO-2, so match either (MatchAny is an OR over the array).
    return [iso2, iso2.upper()]


# ISO-2 (and a couple of common slug tokens) → casefolded country name(s) so a
# ``country_<iso2>`` target id expands to the name a finding actually prints
# ("country_g20_id" → 'indonesia'). The off-target guard checks BOTH the raw
# slug token (matches a finding's ISO-coded ``geo`` tag) and the expanded
# name(s) (matches the country named in the finding text). Covers the G20 tier
# (``country_g20_*``) AND the watch tier (``country_watch_*``: kp/tw/ua/il/ir/pk).
# A slug MISSING here is now SAFE, not silent suppression: ``finding_is_off_target``
# fails OPEN when a desk has no name anchor beyond its bare ISO slug (it cannot
# tell the desk's own country from another's, so it publishes). An entry here
# therefore buys PRECISION — it lets the guard actually catch that desk's
# off-target contamination — but is NOT required to avoid the 100%-TRACE_ONLY
# regression that hit the unmapped kp/tw/ua watch desks. (A miss on the
# OTHER-country gazetteer side likewise only means a borderline finding is
# safely published rather than suppressed.)
_TARGET_SLUG_TO_NAMES: dict[str, tuple[str, ...]] = {
    "us": ("united states", "america", "u.s.", "usa"),
    "cn": ("china",),
    "ru": ("russia",),
    "ir": ("iran",),
    "il": ("israel",),
    "in": ("india",),
    "id": ("indonesia",),
    "br": ("brazil",),
    "ar": ("argentina",),
    "mx": ("mexico",),
    "ca": ("canada",),
    "fr": ("france",),
    "de": ("germany",),
    "it": ("italy",),
    "gb": ("united kingdom", "britain", "uk"),
    "uk": ("united kingdom", "britain"),
    "jp": ("japan",),
    "kr": ("south korea", "korea"),
    "sa": ("saudi arabia",),
    "tr": ("turkey", "turkiye"),
    "au": ("australia",),
    "za": ("south africa",),
    "eu": ("european union",),
    # Watch tier (non-G20 high-consequence desks). il/ir are already covered
    # above; kp/tw/ua were MISSING → their desks self-suppressed to TRACE_ONLY
    # (the P4 pre-push review C1). North Korea findings say "North Korea"/"DPRK".
    "kp": ("north korea", "dprk"),
    "tw": ("taiwan",),
    "ua": ("ukraine",),
    # Pakistan (S1-T2): a nuclear state on the India border. Without this entry
    # the guard runs BLIND for the pk desk — it fails OPEN (publishes) but cannot
    # tell Pakistan from India, so an India-only finding would slip through as a
    # PK product. The mapping buys the precision to catch that off-target shape.
    "pk": ("pakistan",),
    # A3 / DEC-C (2026-07-16): the escalation-risk watch desks. Aliases cover
    # the shapes wire copy actually uses ("Burma", "DRC", "Burkina"); the guard
    # still fails OPEN for anything missing here.
    "sd": ("sudan",),
    "ml": ("mali",),
    "bf": ("burkina faso", "burkina"),
    "ne": ("niger",),
    "cd": ("dr congo", "drc", "democratic republic of the congo", "congo"),
    "mm": ("myanmar", "burma"),
    "ht": ("haiti",),
}


def target_scope_names(target_id: str | None) -> set[str]:
    """The casefolded geo name set a per-country finding must mention to be
    on-target — the geo token(s) lifted from the run's ``target_id`` slug
    (``country_g20_us`` → {'us', 'united states', 'america'}). Empty for a meta /
    no-target run.

    Includes BOTH the raw slug token (an ISO-2 code that matches a finding's
    ISO-coded ``geo`` tag) AND the expanded country name(s) from
    :data:`_TARGET_SLUG_TO_NAMES` (which match the country named in the finding
    TEXT). DB-free; a caller can fold in additional aliases via
    :func:`finding_is_off_target`'s ``extra_target_names``."""
    names: set[str] = set()
    for tok in _target_id_geo_names(target_id):
        tlc = tok.casefold()
        names.add(tlc)
        names.update(_TARGET_SLUG_TO_NAMES.get(tlc, ()))
    return names


def _subject_matches_scope(subject_key: str, names: set[str]) -> bool:
    """Whole-word, casefolded test: does a narrative's ``subject_key`` (the
    lowercased disputed subject) mention any of the target's geo names? Guards
    the per-country ASSESSED NARRATIVES scope — substring-safe ('us' never
    matches inside 'thus'), mirroring the off-target guard's word-boundary
    discipline."""
    subj = subject_key.casefold()
    for name in names:
        if not name:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", subj):
            return True
    return False


def target_country_name(target_id: str | None) -> str | None:
    """The canonical DISPLAY country name for a per-country target id, or ``None``.

    ``country_watch_ir`` → ``"Iran"``; ``country_g20_us`` → ``"United States"``.
    Used to build the FOCUSED ``vector:world_context`` RAG query (a natural
    "<country> <theme>" phrase), which retrieves the target's Factbook-background
    chunks far better than the bare ISO slug ("ir") or the noisy slice-entity pile
    the query used before M22 (live probe: focused geo+theme ~0.60-0.66 on-target
    vs the diluted pile ~0.47-0.59). Returns ``None`` for a meta / no-target /
    non-single-country target (the query then falls back to theme-only, and the
    per-desk country filter still scopes retrieval). Resolves via the same
    :data:`_TARGET_SLUG_TO_NAMES` gazetteer the off-target guard uses; an unmapped
    slug yields ``None`` (never a bare ISO token as a query term)."""
    if not is_per_country_target(target_id):
        return None
    for tok in _target_id_geo_names(target_id):
        names = _TARGET_SLUG_TO_NAMES.get(tok.casefold())
        if names:
            return names[0].title()
    return None


# A country-name token from a finding's text. We match WHOLE words against a
# known-country gazetteer so a substring (e.g. 'us' inside 'thus') never
# false-positives. Kept deliberately small + lowercase — it only has to catch
# the "names ONLY other countries" contamination shape the off-target guard
# guards against, not be an exhaustive geocoder.
def finding_is_off_target(
    *,
    target_id: str | None,
    text: str,
    key_entities: Sequence[str] = (),
    geo: Sequence[str] = (),
    extra_target_names: Sequence[str] = (),
) -> bool:
    """True when a PER-COUNTRY finding names ONLY other countries and not its own
    target (the D4 contamination shape: an Indonesia run that emitted a fully
    US-focused report). Such a finding must be published as TRACE_ONLY, not as a
    country product.

    Decision (conservative, fail-OPEN — only flips on a clear off-target shape):

      * Non-country / no-target run → always False (never gates a meta run).
      * If the finding mentions its OWN target geo (slug token, an
        ``extra_target_names`` alias, or a ``geo`` tag) → on-target, False.
      * Else, if it names at least one OTHER country (in text / key_entities /
        geo) → OFF-target, True.
      * Else (names no country at all — a generic/thin finding) → False; we only
        suppress a finding that is demonstrably ABOUT other countries, never one
        that merely failed to name its own.
    """
    if not is_per_country_target(target_id):
        return False
    own = target_scope_names(target_id) | {
        n.casefold() for n in extra_target_names if isinstance(n, str) and n.strip()
    }
    # Fail-OPEN when there is no country-NAME anchor beyond the bare ISO slug: an
    # unmapped desk slug (absent from _TARGET_SLUG_TO_NAMES, no caller-supplied
    # name) yields own == {slug}, so we cannot tell the desk's OWN country from
    # another's — and MUST NOT silently suppress a desk whose own country we
    # simply do not recognise. This is the C1 regression class (kp/tw/ua were
    # unmapped → their own name read as an off-target mention → 100% TRACE_ONLY).
    # Degrading to permissive keeps "add a desk = register-a-target, no code"
    # HONEST: a gazetteer entry buys PRECISION, it is never required to avoid
    # silent suppression.
    if own <= set(_target_id_geo_names(target_id)):
        return False
    hay_parts: list[str] = []
    if isinstance(text, str):
        hay_parts.append(text)
    for e in key_entities:
        if isinstance(e, str):
            hay_parts.append(e)
    haystack = " ".join(hay_parts)
    haystack_lc = haystack.casefold()
    geo_lc = {g.casefold() for g in geo if isinstance(g, str) and g.strip()}

    def _mentions(name: str) -> bool:
        nlc = name.casefold()
        if nlc in geo_lc:
            return True
        # whole-word / token boundary so 'us' doesn't hit 'thus'.
        return re.search(rf"(?<![a-z0-9]){re.escape(nlc)}(?![a-z0-9])", haystack_lc) is not None

    # On-target if it mentions its own geo anywhere.
    if any(_mentions(n) for n in own if n):
        return False
    # Otherwise: off-target only if it names some OTHER country.
    others = {c for c in _KNOWN_COUNTRY_TOKENS if c not in own}
    return any(_mentions(c) for c in others)


# A small lowercase country gazetteer (ISO-2 + common names) for the off-target
# guard's "names some OTHER country" test. Intentionally not exhaustive — it
# only needs to recognise the country-shaped tokens that drive the D4
# contamination (a per-country run whose finding is entirely about other
# nation-states). Extend as needed; a miss only means a borderline finding is
# (safely) published rather than suppressed.
_KNOWN_COUNTRY_TOKENS: frozenset[str] = frozenset(
    {
        # G20 + frequent geopolitical actors (names + ISO-2 where unambiguous).
        "united states", "america", "u.s.", "usa", "us",
        "china", "russia", "iran", "israel", "ukraine", "india", "indonesia",
        "brazil", "argentina", "mexico", "canada", "france", "germany",
        "italy", "spain", "united kingdom", "britain", "uk", "japan",
        "south korea", "north korea", "korea", "saudi arabia", "turkey",
        "turkiye", "australia", "south africa", "egypt", "pakistan",
        "afghanistan", "iraq", "syria", "lebanon", "yemen", "venezuela",
        "taiwan", "vietnam", "thailand", "philippines", "nigeria",
    }
)


# ---------------------------------------------------------------------------
# Candidate extraction (deterministic, no DB) — target geo + slice entities
# ---------------------------------------------------------------------------


def collect_grounding_candidates(
    inputs: Sequence[Mapping[str, Any]],
    *,
    target_id: str | None,
    scope: Sequence[str],
    static_candidates: Sequence[str] = (),
) -> list[str]:
    """Pull the candidate entity/geo names to ground on, in priority order.

    Reads ONLY the in-memory signal slice (the same rows the runner renders) +
    the run's ``target_id`` — no DB. Returns a de-duplicated, length-capped
    list of canonical-ish names; the resolver matches them against
    ``facts.subject`` / ``nexuses.subject``.

      * ``static_candidates`` → ALWAYS-ON names prepended regardless of slice
        content. For a GLOBAL meta-analyst (world_assessor) whose slice can be
        flooded by a high-volume source, this guarantees the major ongoing
        world-state (active-conflict parties — Iran/US/Israel/…) is grounded
        even when today's slice doesn't surface them. Decouples grounding from
        slice luck.
      * ``target_geo``     → the run's ``target_id`` slug expanded to a country
        name when it looks like a ``country_<name>`` target, plus the most
        common ``geo`` codes/names across the slice rows.
      * ``slice_entities`` → the signal ``tags`` + the NER ``payload.entities`` +
        any analyst ``key_entities``, and a light pass over titles.

    Order matters: ``static_candidates`` then ``target_geo`` come first so a
    tight ``max_facts`` budget spends on the always-on world-state + the
    analyst's own scope before the slice's long tail.
    """
    scope_set = set(scope or ())
    seen: set[str] = set()
    ordered: list[str] = []

    def _add(name: Any, *, min_len: int = _MIN_CANDIDATE_LEN) -> None:
        if not isinstance(name, str):
            return
        n = name.strip()
        if len(n) < min_len:
            return
        # Drop pure-numeric / date-shaped junk tags.
        if n.replace("-", "").replace("/", "").replace(".", "").isdigit():
            return
        key = n.casefold()
        if key in seen:
            return
        seen.add(key)
        ordered.append(n)

    # 0) static_candidates — always-on world-state (no scope gate; prepended).
    for sc in static_candidates or ():
        _add(sc, min_len=2)

    # 1) target_geo — the analyst's own scope.
    if "target_geo" in scope_set:
        # The target-id geo token is EXPLICIT scope (an ISO-2 code like "us"),
        # not NER noise — exempt it from the junk-rejection min-length floor so
        # it can match an ISO-keyed subject; the slice country names below are
        # the primary match.
        for geo_name in _target_id_geo_names(target_id):
            _add(geo_name, min_len=2)
        # Most-frequent geo across the slice (the per-target slice is already
        # geo-narrowed, so the dominant geo is the analyst's country).
        geo_counts: dict[str, int] = {}
        for row in inputs:
            for g in row.get("geo") or []:
                if isinstance(g, str) and g.strip():
                    geo_counts[g.strip()] = geo_counts.get(g.strip(), 0) + 1
        for g, _c in sorted(geo_counts.items(), key=lambda kv: kv[1], reverse=True):
            _add(g)

    # 2) slice_entities — the named figures the slice talks about.
    if "slice_entities" in scope_set:
        for row in inputs:
            tags = row.get("tags") or []
            if isinstance(tags, (list, tuple)):
                for t in tags:
                    # Skip the synthetic provenance tags the narrator stamps.
                    if isinstance(t, str) and not t.startswith(
                        ("target:", "analyst:", "severity:", "g20", "world_assessment")
                    ):
                        _add(t)
            # Lift structured key_entities the narrator may have stamped.
            data = row.get("data")
            if isinstance(data, Mapping):
                for ke in data.get("key_entities") or []:
                    _add(ke)
                # THE bridge fix: NER writes its named entities to
                # ``payload["entities"]`` (list of {text,class,confidence}), NOT
                # to ``tags`` or ``key_entities`` — those are usually empty on a
                # raw ingested signal. Without this, a geopolitical signal in the
                # slice contributes NO candidates, so the resolver never queries
                # the seeded conflict facts/nexuses for the named countries
                # (e.g. an Iran article never grounds the US⇄Iran war nexus).
                # Lift entity TEXT, conf-gated to skip low-confidence NER noise;
                # accept both the {text,...} dict shape and a bare string.
                for ent in data.get("entities") or []:
                    if isinstance(ent, Mapping):
                        conf = ent.get("confidence")
                        if isinstance(conf, (int, float)) and conf < 0.5:
                            continue
                        _add(ent.get("text"))
                    elif isinstance(ent, str):
                        _add(ent)
            if len(ordered) >= _MAX_CANDIDATES:
                break

    return ordered[:_MAX_CANDIDATES]


def _target_id_geo_names(target_id: str | None) -> Iterable[str]:
    """Best-effort geo names from a target id slug (``country_g20_us`` → 'us').

    Conservative: only emits the trailing token of a ``country_*`` /
    ``*_country_*`` slug, which the resolver matches case-insensitively
    against ``facts.subject`` (ISO code or country name). A non-country target
    id yields nothing — the slice ``geo`` codes carry the scope instead.
    """
    if not target_id or not isinstance(target_id, str):
        return ()
    tid = target_id.strip().lower()
    if "country" not in tid:
        return ()
    token = tid.rsplit("_", 1)[-1]
    if len(token) >= 2:
        return (token,)
    return ()


# R-1 — the standing-question backlog candidate fetch (SubstrateGroundingResolver
# .resolve_open_questions). ONE round trip computes, for every OPEN question:
#   * live_reach   — how many LIVE (non-superseded) products trace FORWARD to
#                    it via output_consumption (mig 0106), bounded-BFS to
#                    _OPEN_QUESTION_FORWARD_WALK_MAX_DEPTH hops — the exact
#                    set claim_watch's staleness_debt gauge counts flags
#                    against, so a question with live_reach > 0 is one where
#                    resolving it retires REAL product debt, not backlog
#                    trivia.
#   * desk_salience — the question's target desk's hottest OPEN situation
#                     intensity (0 when the question has no target_id, or its
#                     desk has none) — reuses the SAME situations.intensity_score
#                     signal resolve_situations() grounds other analysts with.
# The final ranking + truncation happen in Python (open_question_priority_key)
# so the ordering is unit-testable without a DB; this SQL only needs to fetch a
# generously-bounded candidate SET, not the final order.
_OPEN_QUESTIONS_BACKLOG_SQL = """
    WITH RECURSIVE walk AS (
        SELECT q.id AS qid, oc.consumer_id, 1 AS depth
          FROM hypotheses q
          JOIN output_consumption oc ON oc.consumed_id = q.id
         WHERE q.status = 'open_question'
        UNION
        SELECT w.qid, oc.consumer_id, w.depth + 1
          FROM output_consumption oc
          JOIN walk w ON oc.consumed_id = w.consumer_id
         WHERE w.depth < $1
    ), reach AS (
        SELECT w.qid,
               count(DISTINCT w.consumer_id) FILTER (
                   WHERE NOT EXISTS (
                       SELECT 1 FROM analyst_outputs ao
                        WHERE ao.id = w.consumer_id
                          AND ao.superseded_by IS NOT NULL
                   )
               ) AS live_reach
          FROM walk w
         GROUP BY w.qid
    ), salience AS (
        SELECT target_id, max(intensity_score) AS max_intensity
          FROM situations
         WHERE status <> 'closed'
           AND (valid_until IS NULL OR valid_until > now())
           AND target_id IS NOT NULL
         GROUP BY target_id
    )
    SELECT q.id, q.thesis, q.target_id, q.produced_at, q.diagnostic_evidence,
           COALESCE(r.live_reach, 0) AS live_reach,
           COALESCE(s.max_intensity, 0.0) AS desk_salience
      FROM hypotheses q
      LEFT JOIN reach r ON r.qid = q.id
      LEFT JOIN salience s ON s.target_id = q.target_id
     WHERE q.status = 'open_question'
     ORDER BY q.produced_at DESC, q.id
     LIMIT $2
"""


# ---------------------------------------------------------------------------
# Substrate resolver — current authoritative facts + signed nexuses
# ---------------------------------------------------------------------------


class SubstrateGroundingResolver:
    """Reads CURRENT authoritative facts/nexuses for a candidate name set.

    Constructed once per grounded analyst by the deps-builder (closing over
    the substrate ``pg_pool``); called per run with the candidate names. The
    current-facts gate matches
    :mod:`legba.runtime.substrate_query_port` — open rows only
    (``superseded_by IS NULL AND (valid_until IS NULL OR valid_until >
    now())``) — and is RESTRICTED to ``source_type IN ('seed','curated')``
    (env-overridable; see the module-level PROVENANCE GATE note) so a
    hallucinated live/ingestion fact is EXCLUDED outright, not merely
    outranked.
    """

    def __init__(
        self,
        *,
        pg_pool: Any,
        target_id: str | None = None,
        embedder: Any | None = None,
        qdrant: Any | None = None,
        world_context_collection: str = "world_context",
    ) -> None:
        self._pool = pg_pool
        # The run's target id, when the deps-builder constructs one resolver per
        # run (per-country path). Optional + backward-compatible: a resolver
        # built without it (the historical call, or a meta analyst) defaults to
        # the GLOBAL view, so world_assessor is untouched. When set to a
        # ``country_*`` id, resolve_graph_structure self-scopes (D4) even if the
        # caller doesn't thread an explicit scope arg.
        self._target_id = target_id
        # L-114 embedder-through-port. The hosted embedding client
        # (:class:`legba.runtime.embedding_factory.HostedEmbeddingClient`,
        # ``async def embed(text) -> list[float]``) the deps-builder threads in
        # when the host has one; None otherwise. The Tier-1 STRUCTURED reads
        # (facts/nexuses/situations/graph-structure) stay vector-free; the
        # embedder powers the S5-T3 opportunistic ``vector:world_context`` RAG
        # (:meth:`resolve_world_context`) — the curated unstructured-brief
        # collection queried semantically (seam #20). Optional + backward-
        # compatible: no embedder → no RAG block (degrade-not-drop).
        self._embedder = embedder
        # S5-T3 — the raw async Qdrant client (``AsyncQdrantClient``, or a
        # ``query_points``/``search``-shaped stand-in) the host built at bring-up,
        # and the collection the Lane-4 loader wrote the ``world_context`` corpus
        # to. Both must be present (with an embedder) for the RAG block to build;
        # any missing → :meth:`resolve_world_context` returns ``[]`` (no block).
        self._qdrant = qdrant
        self._world_context_collection = world_context_collection

    async def resolve(
        self, candidates: Sequence[str], *, max_facts: int,
    ) -> tuple[list[GroundingFact], list[GroundingNexus]]:
        """Return (facts, nexuses) for the candidates, capped at ``max_facts``.

        Facts are the primary budget consumer; nexuses are a small extra
        structural layer (capped at :data:`_MAX_NEXUSES`, never exceeding the
        leftover fact budget). An empty candidate set short-circuits to
        ``([], [])`` so no query runs.
        """
        names = [c for c in candidates if c and c.strip()]
        if not names or max_facts <= 0:
            return [], []

        # Resolve the provenance gate ONCE per run so facts and nexuses share
        # the same trusted-source set (and the env is read a single time).
        trusted = list(trusted_source_types())
        try:
            facts = await self._query_facts(names, trusted=trusted, limit=max_facts)
            nexus_budget = min(_MAX_NEXUSES, max(0, max_facts - len(facts)))
            nexuses = (
                await self._query_nexuses(names, trusted=trusted, limit=nexus_budget)
                if nexus_budget > 0
                else []
            )
            return facts, nexuses
        except Exception as exc:  # degrade-not-drop — grounding is enrichment
            logger.warning("grounding.resolve.failed err=%s", exc)
            return [], []

    async def _query_facts(
        self, names: Sequence[str], *, trusted: Sequence[str], limit: int,
    ) -> list[GroundingFact]:
        # Match any candidate as the fact SUBJECT (case-insensitive, exact —
        # subjects are canonical entity names). Current-facts gate per
        # substrate_query_port. PROVENANCE GATE: only operator-vetted
        # source_type rows ('seed','curated') reach the preamble — ingestion /
        # agent NER junk is dropped wholesale (see the module docstring). The
        # seed/curated ORDER-BY key is kept first so that, if an operator
        # widens the trusted set via env, canonical seed/curated still rank
        # above any added lane.
        lowered = [n.casefold() for n in names]
        # A bare-QID value (``value ~ '^Q[0-9]+$'``) is an unreadable
        # label-lookup failure — exclude it in SQL so the ``LIMIT`` budget is
        # spent only on facts that can actually render. (The Python guard below
        # is the in-process backstop for the same rule.)
        #
        # CONTESTED annotation (Wave 5, #101): LEFT JOIN the already-populated
        # contention SIDECAR so a grounding-eligible fact that is in a live
        # dispute is ANNOTATED (CONTESTED/DISPUTED), never silently asserted as
        # ground truth. Read-only — the join touches nothing, the provenance
        # gate above is unchanged (only seed/curated rows are SELECTed; the
        # sidecar merely tells the renderer one of them is disputed). A
        # ``collapsed`` group (down to one value) reads as uncontested, so the
        # COALESCE folds it back to the marker default — we trust the live
        # sidecar status over a possibly-stale ``facts.contested`` marker.
        # P3-2 preference (default OFF): when opted in, a surfaced winner
        # outranks its contested siblings so the grounded line is the arbiter's
        # pick. OFF by default so analyst consumption is unchanged unless an
        # operator explicitly enables it. The annotation (contested/disputed)
        # is independent and governed by contention_surfacing_enabled().
        prefer_order = (
            "\n              COALESCE(f.surfaced_winner, false) DESC,"
            if contention_prefer_surfaced()
            else ""
        )
        sql = f"""
            SELECT f.subject, f.predicate, f.value, f.valid_from,
                   f.source_type, f.confidence,
                   (f.contested AND fc.status IN ('contested','surfaced'))
                       AS contested,
                   COALESCE(f.surfaced_winner, false) AS surfaced_winner,
                   fc.value_count AS contention_value_count
            FROM facts f
            LEFT JOIN fact_contention fc ON fc.id = f.contention_id
            WHERE lower(f.subject) = ANY($1::text[])
              AND f.source_type = ANY($2::text[])
              AND f.superseded_by IS NULL
              AND (f.valid_until IS NULL OR f.valid_until > now())
              AND f.value !~ '^Q[0-9]+$'
            ORDER BY
              (f.source_type IN ('seed','curated')) DESC,{prefer_order}
              f.confidence DESC NULLS LAST,
              f.valid_from DESC NULLS LAST
            LIMIT $3
        """
        # C4 DECAY WEIGHTING (flag LEGBA_FACT_DECAY_WEIGHTING, default OFF):
        # when ON, ALSO join the fact_decay_states sidecar (the daily
        # fact_decay_scan readout, migration 0098) — a fact whose decayed
        # confidence sits at/below the revoke threshold (decay_state
        # 'revoke_candidate') is EXCLUDED from the ground-truth preamble
        # outright, and decayed_confidence/decay_state annotate the surviving
        # lines. A fact with NO sidecar row yet (scan hasn't run / draft
        # analyst) passes through un-annotated — the seam degrades to the
        # unweighted read, it never drops an unstamped fact. Flag OFF: the
        # ORIGINAL sql above runs untouched, so the read (and the assembled
        # slice) is byte-identical to the pre-C4 behavior.
        if fact_decay_weighting_enabled():
            sql = """
            SELECT f.subject, f.predicate, f.value, f.valid_from,
                   f.source_type, f.confidence,
                   (f.contested AND fc.status IN ('contested','surfaced'))
                       AS contested,
                   COALESCE(f.surfaced_winner, false) AS surfaced_winner,
                   fc.value_count AS contention_value_count,
                   fds.decayed_confidence AS decayed_confidence,
                   fds.decay_state AS decay_state
            FROM facts f
            LEFT JOIN fact_contention fc ON fc.id = f.contention_id
            LEFT JOIN fact_decay_states fds ON fds.fact_id = f.id
            WHERE lower(f.subject) = ANY($1::text[])
              AND f.source_type = ANY($2::text[])
              AND f.superseded_by IS NULL
              AND (f.valid_until IS NULL OR f.valid_until > now())
              AND f.value !~ '^Q[0-9]+$'
              AND (fds.decay_state IS NULL
                   OR fds.decay_state <> 'revoke_candidate')
            ORDER BY
              (f.source_type IN ('seed','curated')) DESC,
              f.confidence DESC NULLS LAST,
              f.valid_from DESC NULLS LAST
            LIMIT $3
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, lowered, list(trusted), int(limit))
        annotate = contention_surfacing_enabled()
        out: list[GroundingFact] = []
        for r in rows:
            # Backstop the SQL guard: never let a bare-QID value through to the
            # preamble (degrade to no-grounding for this fact, not a Qxxxx line).
            if _is_bare_qid(r["value"]):
                continue
            # asyncpg returns the joined columns; a fact with no contention_id
            # has NULL ``contested`` / ``contention_value_count`` (no matched
            # row), which the GroundingFact ctor coerces to the uncontested
            # default. ``_row_get`` keeps a pre-Wave-5 row shape (one that never
            # SELECTed the joined columns) backward-compatible — it degrades to
            # the plain/uncontested default rather than raising.
            # Annotation kill-switch (P3-2): flag OFF renders every fact plainly
            # (uncontested), regardless of the sidecar. Default ON = unchanged.
            contested_raw = _row_get(r, "contested")
            contested = (
                bool(contested_raw) if (annotate and contested_raw is not None) else False
            )
            vc_raw = _row_get(r, "contention_value_count")
            value_count = int(vc_raw) if vc_raw is not None else None
            # C4 decay annotation — the columns exist ONLY on the flag-ON SQL;
            # _row_get degrades a flag-OFF row (or a stub row without them) to
            # None, which renders byte-identically to the pre-C4 line.
            dc_raw = _row_get(r, "decayed_confidence")
            ds_raw = _row_get(r, "decay_state")
            out.append(
                GroundingFact(
                    subject=r["subject"],
                    predicate=r["predicate"],
                    value=r["value"],
                    valid_from=r["valid_from"],
                    source_type=r["source_type"],
                    confidence=(
                        float(r["confidence"]) if r["confidence"] is not None else None
                    ),
                    contested=contested,
                    surfaced_winner=bool(_row_get(r, "surfaced_winner") or False),
                    contention_value_count=value_count,
                    decayed_confidence=(
                        float(dc_raw) if dc_raw is not None else None
                    ),
                    decay_state=(str(ds_raw) if ds_raw is not None else None),
                )
            )
        return out

    async def _query_nexuses(
        self, names: Sequence[str], *, trusted: Sequence[str], limit: int,
    ) -> list[GroundingNexus]:
        # PROVENANCE GATE (mirrors _query_facts): only seed/curated signed
        # relationships reach the preamble. The reified/promoted ``agent``
        # nexus lane (relationship_reifier, proposed_edge_governance) is an
        # analysis product, not ground truth, so it is excluded here.
        lowered = [n.casefold() for n in names]
        sql = """
            SELECT subject, rel_type, object, polarity, valid_from
            FROM nexuses
            WHERE lower(subject) = ANY($1::text[])
              AND source_type = ANY($2::text[])
              AND superseded_by IS NULL
              AND (valid_until IS NULL OR valid_until > now())
            ORDER BY
              (source_type IN ('seed','curated')) DESC,
              confidence DESC NULLS LAST,
              valid_from DESC NULLS LAST
            LIMIT $3
        """
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, lowered, list(trusted), int(limit))
        out: list[GroundingNexus] = []
        for r in rows:
            # A bare-QID subject OR object renders an unreadable edge line
            # ("Q30 member of Q1065") — skip it (degrade to no-grounding for
            # this relationship) for the same reason bare-QID fact values go.
            if _is_bare_qid(r["subject"]) or _is_bare_qid(r["object"]):
                continue
            out.append(
                GroundingNexus(
                    subject=r["subject"],
                    rel_type=r["rel_type"],
                    object=r["object"],
                    polarity=(int(r["polarity"]) if r["polarity"] is not None else None),
                    valid_from=r["valid_from"],
                )
            )
        return out

    async def resolve_situations(
        self, *, scope_target_id: str | None, limit: int,
    ) -> list[GroundingSituation]:
        """Return the OPEN (non-closed) situation frames for the scope, most
        intense first.

        ``scope_target_id`` scopes to one target's situations (a country
        assessor's own frames, matched on the populated ``target_id`` column —
        migration 0042); ``None`` returns the top open frames across all targets
        (the world view). Open = ``valid_until IS NULL`` (the temporal-frame
        gate, migration 0040) AND ``status <> 'closed'`` — so an ongoing-but-
        quiet (dormant) frame is still surfaced as current context, while a
        closed frame is not. Degrade-not-drop: any read failure logs + yields
        ``[]`` (no block). The frame count is clamped to
        :data:`_MAX_SITUATIONS` so a generous caller budget can't flood the
        prompt with the long tail.
        """
        limit = min(int(limit), _MAX_SITUATIONS)
        if limit <= 0:
            return []
        min_intensity = situation_grounding_min_intensity()
        # The intensity floor is an SQL filter; the non-event name filter runs in
        # Python (post-fetch), so over-fetch a little headroom so a dropped junk
        # frame doesn't cost a real frame its slot.
        sql = """
            SELECT name, category, status, intensity_score, valid_from, last_event_at
            FROM situations
            WHERE superseded_by IS NULL
              AND (valid_until IS NULL OR valid_until > now())
              AND status <> 'closed'
              AND intensity_score >= $2
              AND ($1::text IS NULL OR target_id = $1)
            ORDER BY intensity_score DESC NULLS LAST, valid_from DESC NULLS LAST
            LIMIT $3
        """
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    sql, scope_target_id, float(min_intensity), int(limit) + 8,
                )
        except Exception as exc:  # degrade-not-drop — grounding is enrichment
            logger.warning("grounding.resolve_situations.failed err=%s", exc)
            return []
        out: list[GroundingSituation] = []
        for r in rows:
            # Skip clustered non-event frames ("No <geo>-specific … alerts …").
            if _is_non_event_situation(r["name"]):
                continue
            out.append(
                GroundingSituation(
                    name=r["name"],
                    category=r["category"],
                    status=r["status"],
                    intensity_score=(
                        float(r["intensity_score"])
                        if r["intensity_score"] is not None
                        else None
                    ),
                    valid_from=r["valid_from"],
                    last_event_at=r["last_event_at"],
                )
            )
            if len(out) >= limit:
                break
        return out

    async def resolve_narratives(
        self, *, target_id: str | None, limit: int,
    ) -> list[GroundingNarrative]:
        """Return the LIVE reified narratives (mig 0102) for the scope, most
        recently active first — the ASSESSED NARRATIVES grounding source.

        Reads the ``narratives`` sidecar the ``narrative_mapper`` deterministic
        analyst wholesale-refreshes (contested-claim families + publish-order
        propagation detail). ``collapsed`` families are excluded — the block is
        a heads-up on LIVE disputes, not an archive.

        Scope: a PER-COUNTRY run (``country_*`` target) keeps only narratives
        whose ``subject_key`` mentions one of the target's geo names
        (whole-word, casefolded — the ``target_scope_names`` gazetteer), so an
        India desk never grounds against an unrelated country's dispute; the
        match runs in Python over an over-fetched recency window (the
        situations non-event-filter idiom). A meta / no-target run keeps the
        global recency top.

        Degrade-not-drop: any read failure (including the sidecar table not
        existing on a pre-0102 database) logs + yields ``[]`` — no block, never
        an error into the run. Honest empty: an empty sidecar yields ``[]`` and
        the caller renders NO block (no fabricated header).
        """
        limit = min(int(limit), _MAX_NARRATIVES)
        if limit <= 0:
            return []
        scope_names = target_scope_names(target_id) if target_id else set()
        fetch_n = _NARRATIVES_SCOPED_FETCH if scope_names else limit
        sql = """
            SELECT subject_key, predicate_key, status, surfaced_value,
                   variant_count, carrier_source_count,
                   publish_dated_source_count, first_seen_at, last_seen_at,
                   lead_source_id, max_echo_lag_hours
            FROM narratives
            WHERE status <> 'collapsed'
            ORDER BY last_seen_at DESC NULLS LAST
            LIMIT $1
        """
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, int(fetch_n))
        except Exception as exc:  # degrade-not-drop — grounding is enrichment
            logger.warning("grounding.resolve_narratives.failed err=%s", exc)
            return []
        out: list[GroundingNarrative] = []
        for r in rows:
            subject = str(r["subject_key"] or "")
            if scope_names and not _subject_matches_scope(subject, scope_names):
                continue
            out.append(
                GroundingNarrative(
                    subject_key=subject,
                    predicate_key=str(r["predicate_key"] or ""),
                    status=r["status"],
                    surfaced_value=r["surfaced_value"],
                    variant_count=int(r["variant_count"] or 0),
                    carrier_source_count=int(r["carrier_source_count"] or 0),
                    publish_dated_source_count=int(
                        r["publish_dated_source_count"] or 0
                    ),
                    first_seen_at=r["first_seen_at"],
                    last_seen_at=r["last_seen_at"],
                    lead_source_id=r["lead_source_id"],
                    max_echo_lag_hours=(
                        float(r["max_echo_lag_hours"])
                        if r["max_echo_lag_hours"] is not None
                        else None
                    ),
                )
            )
            if len(out) >= limit:
                break
        return out

    async def resolve_open_questions(
        self, *, limit: int,
    ) -> list[GroundingOpenQuestion]:
        """R-1 — the bounded, DETERMINISTICALLY-ordered standing-question
        backlog for a backlog-draining analyst (corpus_researcher).

        ONE round trip: a recursive CTE walks ``output_consumption`` (mig
        0106, bounded to :data:`_OPEN_QUESTION_FORWARD_WALK_MAX_DEPTH` hops —
        the SAME bounded-BFS shape ``claim_watch``'s forward review-flag walk
        uses) to compute each open question's ``live_reach`` — how many
        LIVE (non-superseded) products trace forward to it, i.e. the exact
        set driving ``staleness_debt`` — fused with a ``desk_salience`` read
        (the target desk's hottest OPEN situation intensity, 0 when the
        question carries no target_id or its desk has none). Candidates are
        over-fetched (:data:`_OPEN_QUESTION_CANDIDATE_FETCH_CAP`, comfortably
        above the live backlog size) so the safety cap can never silently
        exclude a genuinely higher-priority question; the actual ranking
        (:func:`open_question_priority_key`) and truncation to ``limit``
        (hard-capped at :data:`_MAX_OPEN_QUESTIONS_GROUNDING`) happen in pure
        Python — deterministic, reproducible, unit-testable without a DB.

        Degrade-not-drop: any read failure logs + returns ``[]`` — the caller
        (:func:`build_open_questions_block`) then renders no block, and the
        analyst falls back to self-selection, BYTE-IDENTICAL to its behavior
        before this source existed.
        """
        limit = min(max(0, int(limit)), _MAX_OPEN_QUESTIONS_GROUNDING)
        if limit <= 0:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    _OPEN_QUESTIONS_BACKLOG_SQL,
                    _OPEN_QUESTION_FORWARD_WALK_MAX_DEPTH,
                    _OPEN_QUESTION_CANDIDATE_FETCH_CAP,
                )
        except Exception as exc:  # degrade-not-drop — grounding is enrichment
            logger.warning("grounding.resolve_open_questions.failed err=%s", exc)
            return []

        now = datetime.now(timezone.utc)
        ranked: list[tuple[tuple, GroundingOpenQuestion]] = []
        for r in rows:
            produced_at = r["produced_at"]
            age_days = 0.0
            if isinstance(produced_at, datetime):
                ref = (
                    produced_at if produced_at.tzinfo
                    else produced_at.replace(tzinfo=timezone.utc)
                )
                age_days = max(0.0, (now - ref).total_seconds() / 86400.0)
            harvest_class = harvest_class_of(r["diagnostic_evidence"])
            live_reach = int(r["live_reach"] or 0)
            desk_salience = float(r["desk_salience"] or 0.0)
            key = open_question_priority_key(
                live_reach=live_reach,
                harvest_class=harvest_class,
                desk_salience=desk_salience,
                age_days=age_days,
                question_id=str(r["id"]),
            )
            ranked.append((
                key,
                GroundingOpenQuestion(
                    id=r["id"],
                    thesis=str(r["thesis"] or ""),
                    harvest_class=harvest_class,
                    target_id=r["target_id"],
                    produced_at=produced_at,
                    live_reach=live_reach,
                    desk_salience=desk_salience,
                ),
            ))
        ranked.sort(key=lambda pair: pair[0])
        return [gq for _key, gq in ranked[:limit]]

    async def resolve_graph_structure(
        self, candidates: Sequence[str], *, limit: int,
        scope_target_id: str | None = None,
    ) -> "GroundingGraphStructure | None":
        """Return the headline interesting STRUCTURES the knowledge graph
        surfaced (most-tense actors, brokers, proxy chains) for the ASSESSED
        STRUCTURE block, prioritised to the assessor's candidate entities.

        Reads the LATEST ``structural_balance`` + ``graph_mining`` graph_metrics
        rows (the interesting-edge math already runs each cadence; this is the
        first reader — closing the compute→consume gap). PREFERS the producers'
        own ranked ``interesting`` shortlist (the shared contract) when present —
        each item carries a kind + rationale + score, so the block reads as a
        curated shortlist; falls back to the raw frustration/betweenness/proxy
        extraction for older payloads that lack it. The metrics are global (no
        per-target column), so we scope by candidate NAME. Degrade-not-drop: any
        read/parse failure logs + yields ``None`` (no block). Returns ``None``
        when nothing notable renders so the caller skips an empty header.

        D4 CONTAMINATION FIX — per-country scoping. When this is a PER-COUNTRY
        run (``scope_target_id`` is a ``country_*`` id, or the resolver was built
        with one), out-of-scope global structures are DROPPED rather than used to
        top up the limit, so a country_assessor never inherits the globally-most-
        central (US-centric) structure. ``scope_target_id`` defaults to ``None``
        and, when ``None``, falls back to the resolver's own ``target_id`` — so a
        caller that doesn't thread the arg still self-scopes when the resolver was
        constructed per-country, and a META / no-target run (no target either
        place) keeps the GLOBAL block unchanged.
        """
        per_cat = min(int(limit), _MAX_GRAPH_STRUCTURE)
        if per_cat <= 0:
            return None
        cand_lc = {c.casefold() for c in candidates if c and c.strip()}
        # Per-country path drops the global tail; meta / no-target keeps it.
        effective_target = scope_target_id if scope_target_id is not None else self._target_id
        target_scoped = is_per_country_target(effective_target)
        # DISTINCT ON keeps only the freshest row per metric_kind.
        sql = """
            SELECT DISTINCT ON (metric_kind) metric_kind, payload
            FROM graph_metrics
            WHERE metric_kind IN ('structural_balance', 'graph_mining')
            ORDER BY metric_kind, computed_at DESC
        """
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql)
        except Exception as exc:  # degrade-not-drop — grounding is enrichment
            logger.warning("grounding.resolve_graph_structure.failed err=%s", exc)
            return None
        payloads = {r["metric_kind"]: _json_or_dict(r["payload"]) for r in rows}
        # PREFER the shared ``interesting`` shortlist. Cap at 2× the per-category
        # bound so the merged (cross-metric, multi-kind) list keeps roughly the
        # same overall footprint as the legacy 3-category fallback.
        interesting = _collect_interesting(
            payloads, cand_lc, per_cat * 2, target_scoped=target_scoped,
        )
        if interesting:
            structure = GroundingGraphStructure(
                frustration=[], brokers=[], proxy_chains=[], interesting=interesting,
            )
            return None if structure.is_empty() else structure
        # FALLBACK — raw extraction from the metric maps (no shortlist present).
        sb = payloads.get("structural_balance") or {}
        gm = payloads.get("graph_mining") or {}
        structure = GroundingGraphStructure(
            frustration=_top_graph_items(
                sb.get("frustration"), cand_lc, per_cat, min_value=0.0,
                target_scoped=target_scoped,
            ),
            brokers=_top_brokers(
                gm.get("top_centrality"), cand_lc, per_cat, target_scoped=target_scoped,
            ),
            proxy_chains=_top_proxy_chains(
                gm.get("proxy_chains"), cand_lc, per_cat, target_scoped=target_scoped,
            ),
        )
        return None if structure.is_empty() else structure

    async def resolve_world_context(
        self, query: str, *, limit: int, target_id: str | None = None,
    ) -> list[GroundingWorldContextChunk]:
        """Opportunistic RAG (S5-T3) — semantic hits from the ``world_context``
        Qdrant collection for the BACKGROUND PRIORS block.

        ``query`` is the ASSEMBLE-time RAG query (the unit's bounded question +
        target country + top slice entities). Returns up to
        :data:`_MAX_WORLD_CONTEXT_CHUNKS` retrieved chunks, most-similar first.

        TWO cheap retrieval guards (the prerequisite for safe RAG expansion):

          * RELEVANCE FLOOR — a retrieved chunk must clear
            :func:`world_context_min_score` (default 0.55, M22-calibrated) to
            ground. Applied
            server-side via Qdrant's ``score_threshold`` AND re-checked
            client-side (a stub / client that ignores the threshold still can't
            leak a below-floor chunk). If ALL hits fall below → no chunks → no
            block (degrade-not-drop).
          * COUNTRY FILTER — for a single-country desk (``target_id`` resolves to
            one ISO-2 via :func:`world_context_country_filter_values`), a Qdrant
            ``MatchAny`` payload filter over the chunk ``countries`` array
            restricts retrieval to THAT country's chunks (a France desk can't pull
            an Iran chunk). A meta / no-target / non-single-country target applies
            NO filter — the global picture is legitimate.

        HONESTY + degrade-not-drop, all yielding ``[]`` (→ no BACKGROUND PRIORS
        block, so no fabricated header):

          * no embedder OR no Qdrant client wired (the vector plane wasn't
            provisioned) → ``[]``;
          * an empty / whitespace query → ``[]`` (no embed round-trip);
          * an EMPTY collection (the corpus hasn't been loaded) → the search
            returns no hits → ``[]``;
          * an embed-backend failure or a Qdrant error → logged + ``[]`` (RAG is
            an enrichment, never fails the analyst run).

        The retrieved text is PRIOR, not EVIDENCE: it is rendered non-citable and
        carries no signal ids, so verify semantics are untouched by construction.
        """
        if self._embedder is None or self._qdrant is None:
            return []
        q = (query or "").strip()
        if not q:
            return []
        limit = min(int(limit), _MAX_WORLD_CONTEXT_CHUNKS)
        if limit <= 0:
            return []
        # The two guards, both derived WITHOUT raising: the relevance floor (a
        # cosine-similarity minimum) and the per-desk country filter (None for a
        # legitimate global / non-single-country run).
        min_score = world_context_min_score()
        country_values = world_context_country_filter_values(target_id)
        # Embed the RAG query. An embed-backend failure degrades to no block
        # (never a fabricated vector) — the same contract vector_search honors.
        try:
            vec = await self._embedder.embed(q)
        except Exception as exc:  # degrade-not-drop — RAG is enrichment
            logger.warning("grounding.resolve_world_context.embed_failed err=%s", exc)
            return []
        try:
            hits = await self._search_world_context(
                vec, limit=limit, min_score=min_score, country_values=country_values,
            )
        except Exception as exc:  # degrade-not-drop — RAG is enrichment
            logger.warning("grounding.resolve_world_context.search_failed err=%s", exc)
            return []
        return self._map_world_context_hits(hits, min_score=min_score)

    @staticmethod
    def _world_context_country_filter(country_values: Sequence[str] | None) -> Any | None:
        """A Qdrant ``MatchAny`` payload filter over ``countries`` for the given
        ISO-2 values, or ``None`` when there is nothing to filter on.

        The qdrant models import is LOCAL (mirrors ``substrate_query_port``) so
        the grounding module never hard-depends on the client at import time; a
        filter-build failure degrades to ``None`` (no filter) rather than raising
        on the grounding path.
        """
        if not country_values:
            return None
        try:
            from qdrant_client.http import models as qmodels

            return qmodels.Filter(
                must=[
                    qmodels.FieldCondition(
                        key="countries",
                        match=qmodels.MatchAny(any=list(country_values)),
                    )
                ]
            )
        except Exception as exc:  # degrade-not-drop — no filter beats a raise
            logger.warning("grounding.world_context.filter_build_failed err=%s", exc)
            return None

    async def _search_world_context(
        self,
        query_embedding: Sequence[float],
        *,
        limit: int,
        min_score: float | None = None,
        country_values: Sequence[str] | None = None,
    ) -> list[Any]:
        """Cosine-search the ``world_context`` collection by raw vector.

        Applies the relevance floor server-side (``score_threshold``) and the
        optional per-desk country filter (``query_filter`` — MatchAny over the
        chunk ``countries`` array) so a real Qdrant never even returns a
        below-floor or off-country chunk. Supports both the modern ``query_points``
        (qdrant-client >= 1.10, returns a response with ``.points``) and the legacy
        ``search`` surface, mirroring :meth:`legba.data.qdrant.QdrantStore.search`
        / ``substrate_query_port.vector_search_by_embedding`` so this isn't pinned
        to one client version.
        """
        vec = list(query_embedding)
        query_filter = self._world_context_country_filter(country_values)
        threshold = float(min_score) if min_score is not None else None
        if hasattr(self._qdrant, "query_points"):
            resp = await self._qdrant.query_points(
                collection_name=self._world_context_collection,
                query=vec,
                limit=int(limit),
                query_filter=query_filter,
                score_threshold=threshold,
                with_payload=True,
            )
            return list(getattr(resp, "points", None) or [])
        hits = await self._qdrant.search(  # pragma: no cover — legacy client
            collection_name=self._world_context_collection,
            query_vector=vec,
            limit=int(limit),
            query_filter=query_filter,
            score_threshold=threshold,
            with_payload=True,
        )
        return list(hits or [])

    def _map_world_context_hits(
        self, hits: Sequence[Any], *, min_score: float | None = None,
    ) -> list[GroundingWorldContextChunk]:
        """Map raw Qdrant hits onto :class:`GroundingWorldContextChunk`.

        Reads the Lane-4 payload shape (``text`` / ``title`` / ``section`` /
        ``source_url`` — see :func:`legba.data.rag.lane4_loader._build_payload`).
        A hit with no readable ``text`` is dropped (an empty chunk contributes no
        prior); the Qdrant point id becomes the auditable ``chunk_id``.

        RELEVANCE FLOOR backstop: when ``min_score`` is set, a hit whose (readable)
        cosine ``score`` is below the floor is dropped even if the client / stub
        ignored the server-side ``score_threshold``. A hit with no readable score
        is trusted (it came back from an already-thresholded query) rather than
        dropped — degrade-not-drop, never over-suppress.
        """
        out: list[GroundingWorldContextChunk] = []
        for hit in hits or []:
            hid = getattr(hit, "id", None)
            if hid is None:
                continue
            payload = getattr(hit, "payload", None) or {}
            if not isinstance(payload, Mapping):
                continue
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            score = getattr(hit, "score", None)
            score_f = float(score) if isinstance(score, (int, float)) else None
            if (
                min_score is not None
                and score_f is not None
                and score_f < float(min_score)
            ):
                continue
            out.append(
                GroundingWorldContextChunk(
                    chunk_id=str(hid),
                    title=payload.get("title") if isinstance(payload.get("title"), str) else None,
                    section=payload.get("section") if isinstance(payload.get("section"), str) else None,
                    source_url=(
                        payload.get("source_url")
                        if isinstance(payload.get("source_url"), str)
                        else None
                    ),
                    text=text,
                    score=score_f,
                )
            )
        return out


# ---------------------------------------------------------------------------
# Preamble assembly — the dated "AUTHORITATIVE CURRENT CONTEXT" block
# ---------------------------------------------------------------------------


_PREAMBLE_HEADER = (
    "AUTHORITATIVE CURRENT CONTEXT (as of {today} — treat as ground truth over "
    "any prior knowledge; these facts are current as of today and SUPERSEDE "
    "anything your training data implies about who currently holds office, "
    "which alliances are in force, or the present state of the world):"
)


# M13 (2026-07-06) — a curated CURRENT-officeholder anchor, ALWAYS rendered at the
# top of the ground-truth preamble (independent of slice / candidate luck) so a
# grounded assessor never back-fills a stale-cutoff officeholder from its training
# prior (the "former President Trump" error class — a live RU narrative_coordination
# finding called the SITTING US president "former"). Minimal by design: the single
# clear live stale-cutoff error (the US presidency). The seed already carries the
# same fact (Donald Trump | leader of | United States, valid_until NULL); this
# anchor guarantees it reaches the preamble even when "United States" is not a
# resolved grounding candidate for the run. MIRRORS the lexical write/verify-time
# guard's curated map in legba.data.provenance.verify (_CURRENT_OFFICEHOLDERS) —
# keep the two in lockstep (extend only for a NEW confirmed live stale-leader error).
_CURRENT_OFFICEHOLDER_ANCHOR: tuple[str, ...] = (
    "United States — current President (head of state and government): Donald Trump "
    "(in office since 2025-01-20; the SITTING president — do NOT refer to him as a "
    "former president).",
)


def build_grounding_preamble(
    facts: Sequence[GroundingFact],
    nexuses: Sequence[GroundingNexus],
    *,
    now: datetime | None = None,
) -> str | None:
    """Render current facts + signed nexuses into a dated preamble block.

    Returns ``None`` when there is nothing to inject (no facts AND no
    nexuses) so the caller can skip prepending an empty header. The block is
    plain text (the runner concatenates it ahead of the rendered slice); it is
    intentionally compact — one line per fact/relationship. When a preamble IS
    built, the curated current-officeholder anchor (M13) heads the fact list.
    """
    if not facts and not nexuses:
        return None
    today = (now or datetime.now(tz=timezone.utc)).date().isoformat()
    lines: list[str] = [_PREAMBLE_HEADER.format(today=today)]
    # M13: curated current-officeholder anchor first (always present when a
    # preamble is built) so the current US president is never mis-stated as former.
    for anchor in _CURRENT_OFFICEHOLDER_ANCHOR:
        lines.append(f"- {anchor}")
    for f in facts:
        lines.append(f"- {f.render()}")
    if nexuses:
        lines.append("Key current relationships:")
        for n in nexuses:
            lines.append(f"- {n.render()}")
    lines.append("")  # blank separator before the slice
    return "\n".join(lines)


# The ASSESSED SITUATIONS block is rendered SEPARATELY from (and after) the
# AUTHORITATIVE CURRENT CONTEXT ground-truth block, with a header that makes the
# trust boundary explicit: situations are analysis DERIVED from clustered
# findings (the system's own situational picture), NOT operator-vetted ground
# truth. Per the Phase-5 operator decision, situations are NEVER laundered into
# the ground-truth block.
_SITUATIONS_HEADER = (
    "ASSESSED SITUATIONS (the system's current situational picture — ONGOING "
    "frames the platform has CLUSTERED from recent findings, NOT operator-vetted "
    "ground truth; use them for continuity/context and weigh accordingly, do not "
    "treat as established fact):"
)


def build_situations_block(
    situations: Sequence[GroundingSituation],
    *,
    now: datetime | None = None,
) -> str | None:
    """Render ongoing situation frames into the dedicated ASSESSED SITUATIONS
    block (analysis-derived, clearly fenced off from ground truth).

    Returns ``None`` when there is nothing to inject so the caller skips an
    empty header. One line per frame; intentionally compact. ``now`` is
    threaded to each frame's staleness render so the "last activity Nd ago"
    age is deterministic in tests.
    """
    if not situations:
        return None
    lines: list[str] = [_SITUATIONS_HEADER]
    for s in situations:
        lines.append(f"- {s.render(now=now)}")
    lines.append("")  # blank separator before the slice
    return "\n".join(lines)


# The ASSESSED NARRATIVES block — the reified contested-claim families (mig
# 0102) the narrative_mapper derives from the contention sidecar + carrier
# lineage, rendered for the narrative_coordination unit (and any other opted-in
# analyst). Like ASSESSED SITUATIONS it is analysis-DERIVED, NOT operator-vetted
# ground truth, so it is rendered in its OWN fenced block and NEVER laundered
# into the AUTHORITATIVE block. The header carries the mapper's honesty
# contract verbatim: detect-only; echo-lead is DESCRIPTIVE publish-order
# timing, never itself a coordination/copying claim.
_NARRATIVES_HEADER = (
    "ASSESSED NARRATIVES (detect-only, analysis-derived — contested-claim "
    "families the platform reified from its own contention sidecar plus "
    "publish-order carrier timing; NOT operator-vetted ground truth. "
    "Echo/lead ordering is DESCRIPTIVE: 'source B published after source A' "
    "is a timing observation, never by itself evidence of copying or "
    "coordination. Use these to see WHICH claims are currently contested and "
    "how they spread; do not treat any variant as established fact):"
)


def build_narratives_block(
    narratives: Sequence[GroundingNarrative],
) -> str | None:
    """Render the reified-narrative rows into the dedicated ASSESSED NARRATIVES
    block (analysis-derived, clearly fenced off from ground truth).

    Returns ``None`` when there is nothing to inject — the honest empty state:
    no narratives ⇒ no header, no fabricated 'no narratives' filler. One
    compact line per narrative; bounded upstream by :data:`_MAX_NARRATIVES`.
    """
    if not narratives:
        return None
    lines: list[str] = [_NARRATIVES_HEADER]
    for n in narratives:
        lines.append(f"- {n.render()}")
    lines.append("")  # blank separator before the slice
    return "\n".join(lines)


# R-1 — the STANDING OPEN QUESTIONS block (the corpus_researcher backlog
# source). Analysis-derived (harvested / unit-flagged), NEVER laundered into
# the ground-truth block. The header carries the field-name contract
# ("addressed_question") the descriptor's own system prompt also documents —
# reinforcement, not the sole source of truth (mirrors how the world_context
# block's own header reinforces its "do not cite" contract) — and the honesty
# discipline: preferring the backlog is a NUDGE, not a requirement, and a null
# result is a legitimate, complete finding in its own right.
_OPEN_QUESTIONS_HEADER = (
    "STANDING OPEN QUESTIONS (backlog — the system's own unresolved analytical "
    "questions, priority-ordered; analysis-derived, NOT ground truth, NOT a "
    "topic you are forced to force an answer to). If one of these can be "
    "meaningfully investigated against the corpus, PREFER IT over "
    "self-selecting a topic, and set the top-level \"addressed_question\" "
    "field in your response to its exact tag (e.g. \"Q2\"). A NULL RESULT is a "
    "legitimate, complete finding: if the corpus does not contain the evidence "
    "to resolve a question, say so plainly — that is evidence about OUR "
    "COLLECTION, not a failure. If none of these apply, self-select a topic as "
    "usual and omit the field:"
)


def build_open_questions_block(
    questions: Sequence[GroundingOpenQuestion],
    *,
    now: datetime | None = None,
) -> str | None:
    """Render the priority-ordered standing-question backlog into the
    dedicated STANDING OPEN QUESTIONS block (analysis-derived, clearly fenced
    off from ground truth).

    Returns ``None`` when there is nothing to inject — the honest empty
    state: no open questions ⇒ no header, no fabricated filler, and the
    caller's analyst falls back to self-selection UNCHANGED. One numbered
    line per question, tagged ``[Q1]``, ``[Q2]``, … in the CALLER's supplied
    order (the priority order :meth:`SubstrateGroundingResolver
    .resolve_open_questions` already computed) — the tag is positional, so
    the caller's sink (mapping tag -> hypothesis id) must be built from the
    SAME ``questions`` sequence, in the SAME order, as this render.
    """
    if not questions:
        return None
    lines: list[str] = [_OPEN_QUESTIONS_HEADER]
    for i, q in enumerate(questions, start=1):
        lines.append(f"- {q.render(tag=f'Q{i}', now=now)}")
    lines.append("")  # blank separator before the slice
    return "\n".join(lines)


# The ASSESSED STRUCTURE block — the knowledge graph's own interesting structures
# (tense actors, brokers, proxy chains) rendered into the assessor prompt. Like
# ASSESSED SITUATIONS it is analysis-DERIVED (computed from the signed nexus
# graph), NOT operator-vetted ground truth, so it is rendered in its OWN fenced
# block and NEVER laundered into the AUTHORITATIVE block. This is the consume-side
# of "use the graph in analysis to find the interesting edges": the structural
# math (structural_balance / graph_mining) already runs each cadence; this block
# is the first thing that puts its output in front of the reasoning LLM.
_GRAPH_STRUCTURE_HEADER = (
    "ASSESSED STRUCTURE (analysis-derived — the system's current knowledge-graph "
    "structure, computed from the signed relationship graph; NOT operator-vetted "
    "ground truth. Use it to NOTICE who is structurally central, tense, or "
    "brokering between camps — and let it sharpen the assessment — but weigh it as "
    "a derived signal, not established fact):"
)


# Human group labels for the ``interesting`` shortlist kinds (the shared
# contract). An unknown kind falls back to a de-underscored version of itself.
_INTERESTING_KIND_LABELS: dict[str, str] = {
    "tense_actor": "Most structurally tense actors (caught in conflicting / unbalanced ties)",
    "broker": "Key brokers (high betweenness — sit between otherwise-separated camps)",
    "new_hostile_edge": "Newly-hostile relationships",
    "sign_imbalanced_triad": "Sign-imbalanced triads (a structurally unstable trio)",
    "proxy_chain": "Notable indirect / proxy links (one actor acts on another via an intermediary)",
}


def build_graph_structure_block(structure: "GroundingGraphStructure | None") -> str | None:
    """Render the knowledge graph's interesting structures into the dedicated
    ASSESSED STRUCTURE block (analysis-derived, clearly fenced off from ground
    truth). Returns ``None`` when there is nothing notable to inject so the caller
    skips an empty header. Compact; bounded by :data:`_MAX_GRAPH_STRUCTURE`.
    """
    if structure is None or structure.is_empty():
        return None
    lines: list[str] = [_GRAPH_STRUCTURE_HEADER]
    # PREFERRED: render the producers' ranked ``interesting`` shortlist, grouped
    # by kind (stable order), each item with its one-line rationale. When the
    # shortlist is present the raw frustration/broker/proxy enumeration is empty,
    # so only this branch fires.
    if structure.interesting:
        by_kind: dict[str, list[GroundingInterestingItem]] = {}
        for it in structure.interesting:
            by_kind.setdefault(it.kind, []).append(it)
        # Known kinds in their canonical order first, then any extra kinds.
        ordered_kinds = [k for k in _INTERESTING_KIND_ORDER if k in by_kind]
        ordered_kinds += [k for k in by_kind if k not in _INTERESTING_KIND_ORDER]
        for kind in ordered_kinds:
            label = _INTERESTING_KIND_LABELS.get(kind, kind.replace("_", " "))
            lines.append(f"- {label}:")
            for it in by_kind[kind]:
                detail = f" — {it.rationale}" if it.rationale else ""
                lines.append(f"  - {it.label}{detail}")
        lines.append("")  # blank separator before the slice
        return "\n".join(lines)
    if structure.frustration:
        rendered = ", ".join(f"{n} ({int(v)})" for n, v in structure.frustration)
        lines.append(
            "- Most structurally tense actors (caught in the most sign-imbalanced / "
            f"conflicting ties): {rendered}"
        )
    if structure.brokers:
        rendered = ", ".join(f"{n} ({v:.3f})" for n, v in structure.brokers)
        lines.append(
            "- Key brokers (high betweenness — sit between otherwise-separated "
            f"camps; a natural conduit or chokepoint): {rendered}"
        )
    if structure.proxy_chains:
        lines.append("- Notable indirect / proxy links (A acts on B through an intermediary):")
        for c in structure.proxy_chains:
            lines.append(f"  - {c}")
    lines.append("")  # blank separator before the slice
    return "\n".join(lines)


# The BACKGROUND PRIORS block (S5-T3 opportunistic RAG) — curated unstructured
# priors retrieved from the ``world_context`` vector corpus. Rendered BELOW the
# AUTHORITATIVE CURRENT CONTEXT ground-truth block, in its OWN fenced block whose
# header makes the trust boundary explicit: this text is PRIOR, not EVIDENCE. It
# frames HOW the unit reasons (method / what to look for), never WHAT is true, and
# is NOT citable via ``[N]`` — every claim still cites the numbered signals, so
# verify semantics are untouched by construction (the honesty rule, RAG plan §B).
# The EXACT operator-facing header string is preserved verbatim as the leading
# clause so a trace / prompt audit can grep for it.
# DQ Phase-2 RAG tune (2026-07-03): the mechanism finding showed the faithfulness
# cost was UNCITED INTERPRETATION, not fact-leak — the model used a prior to write
# an inferred judgement ("the most plausible mechanism is …") with no [N]. The old
# header forbade stating an uncited "fact"; it now forbids an uncited fact OR
# INTERPRETATION, leads with the rule, and names the exact leak shape. The leading
# "BACKGROUND PRIORS (context, not evidence — do not cite)" clause is preserved
# verbatim for trace/prompt-audit grep-ability.
_WORLD_CONTEXT_HEADER = (
    "BACKGROUND PRIORS (context, not evidence — do not cite) — READ THIS RULE "
    "FIRST: never state any fact OR INTERPRETATION drawn from these priors in your "
    "assessment unless a numbered signal [N] supports it. An uncited prior-derived "
    "claim — a fact OR an inferred judgement (e.g. \"the most plausible mechanism "
    "is …\", \"this typically leads to …\") — WILL be scored unfaithful by the "
    "verify pass and demote the finding. The text below is curated background / "
    "doctrine; use it ONLY to frame HOW you read the numbered signals (method, what "
    "to look for), NOT as a source of fact. It is NOT citable. Cite ONLY the "
    "numbered signals, for every claim, exactly as before:"
)

# Closing reminder rendered AFTER the priors, right before the signal slice — a
# last cue at the point the model starts composing (DQ Phase-2 tune).
_WORLD_CONTEXT_FOOTER = (
    "(End of BACKGROUND PRIORS — reminder: nothing above is evidence or citable; "
    "every claim in your assessment must rest on a numbered signal [N].)"
)


def build_world_context_block(
    chunks: Sequence[GroundingWorldContextChunk],
    *,
    block_char_cap: int = _WORLD_CONTEXT_BLOCK_CHAR_CAP,
    chunk_char_cap: int = _WORLD_CONTEXT_CHUNK_CHAR_CAP,
) -> str | None:
    """Render retrieved ``world_context`` chunks into the dedicated BACKGROUND
    PRIORS block (non-citable prior, fenced off from ground truth AND evidence).

    Returns ``None`` when there is nothing to inject (no chunks) so the caller
    skips an empty header — an EMPTY collection yields NO block, never a
    fabricated header. Token-capped: each chunk body is trimmed to
    ``chunk_char_cap`` and the fold stops once the accumulated block would exceed
    ``block_char_cap`` (so the priors can never crowd out the authoritative
    context + the signal slice).
    """
    if not chunks:
        return None
    lines: list[str] = [_WORLD_CONTEXT_HEADER]
    total = len(_WORLD_CONTEXT_HEADER)
    rendered_any = False
    for c in chunks:
        line = c.render(char_cap=chunk_char_cap)
        # Stop once folding this chunk would blow the block cap — but always
        # admit at least one chunk so a single long prior still surfaces (trimmed
        # by the per-chunk cap) rather than yielding an empty header.
        if rendered_any and block_char_cap > 0 and total + len(line) + 1 > block_char_cap:
            break
        lines.append(line)
        total += len(line) + 1
        rendered_any = True
    lines.append(_WORLD_CONTEXT_FOOTER)  # DQ Phase-2: closing cite-only reminder
    lines.append("")  # blank separator before the slice
    return "\n".join(lines)
