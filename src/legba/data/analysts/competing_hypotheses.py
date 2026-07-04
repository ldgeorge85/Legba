# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``competing_hypotheses`` — the PIECE C ACH (Analysis of Competing Hypotheses) kind.

A META analyst kind (sibling of ``relationship_reifier`` /
``meta_findings_synthesizer``) that re-homes the old Legba's ACH rigor into the
source-first / Dapr-actor / analyst-kind architecture. It is the REAL hypotheses
producer (the situation-gated ``hypothesis_lifecycle`` deterministic handler
emitted 0 rows because every situation went dormant — see
``planning/DATA_ANALYSIS_DEEP_REVIEW_2026-06-16.md`` §1.2 / §2 Rank 3). This kind
reads the evidence base DIRECTLY (it is NOT gated on ``active`` situations), so it
fires whenever there is a focal topic with current evidence.

What it does, once per cadence tick (one global sweep — META analyst), for each
focal situation/topic:

  1. **READ** the temporally-CURRENT evidence base for the focal topic:
       * facts where ``superseded_by IS NULL`` (Piece B made facts temporally
         honest — "what is true now" is the open-row query),
       * findings linked to the topic's situation (``analyst_outputs``,
         ``kind='finding'``),
       * the focal situation row itself, and
       * the new signed, typed ``nexuses`` (Piece A) bearing on the topic's
         entities.
  2. **GENERATE** >=2 COMPETING hypotheses about the topic, each with a MANDATORY
     counter-thesis (confirmation bias is structurally impossible — the old ACH
     invariant). The LLM proposes the hypothesis SET; degrade-not-drop falls back
     to a deterministic escalate/de-escalate/status-quo triad when the LLM is
     unavailable or its output is unusable, so the matrix still gets built.
  3. **SCORE the ACH evidence x hypothesis MATRIX**: every evidence item is scored
     for CONSISTENCY with each hypothesis (CC / C / N / I / II in Heuer's notation,
     mapped to +2..-2). Then each item's DIAGNOSTICITY is computed: evidence that
     DISCRIMINATES between the hypotheses (high spread of consistency across
     hypotheses) weighs MORE; evidence consistent with every hypothesis is
     non-diagnostic and weighs ~0 (the core ACH insight — you reason from the
     evidence that *separates* hypotheses, not the evidence that fits all).
  4. **COMPUTE the integer EVIDENCE BALANCE** per hypothesis (the
     diagnosticity-weighted, sign-of-consistency count — robust to confidence
     gaming, the old ACH invariant) and AUTO-TRANSITION the LEAD hypothesis
     ``active -> confirmed`` (balance >= +K) / a dominated one ``active ->
     refuted`` (balance <= -K).
  5. **WRITE** one ``OutputKind.HYPOTHESIS`` row per hypothesis via the LIVE
     ``write_hypothesis`` path (the ``hypotheses`` table + ``OutputKind.HYPOTHESIS``
     already exist — REUSED, no new write plumbing). Each row carries the ACH
     structure in its payload: ``thesis`` + ``counter_thesis`` (hot columns) and
     the full matrix / balance / diagnosticity / per-hypothesis scoring under
     ``diagnostic_evidence`` (the jsonb column the schema already has). The
     per-run ``FindingPayload`` summary (topics / hypotheses / confirmed / refuted)
     is the cadence receipt — exactly how ``relationship_reifier`` /
     ``situation_clustering`` side-write their rows + return a FINDING summary.

Discipline (the ``relationship_reifier`` precedent):

  * **never litellm** — the LLM is reached through the analyst provider plane
    (``deps.llm.chat_complete``); the kind never imports litellm/dspy.
  * **budget-gate + degrade-not-drop** — the run checks
    ``deps.budget.check_envelope()`` before each LLM call and STOPS issuing new
    calls once the envelope is exhausted; any per-topic LLM/parse failure flips
    the run's ``degraded`` counter and falls back to the deterministic hypothesis
    set, so the matrix + balance + rows still land. The LLM is an ENRICHMENT, not
    a hard dependency.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from uuid import UUID, uuid4

from ..provenance.models import FindingPayload, HypothesisPayload
from ..provenance.writes import write_hypothesis
from ...runtime.analyst_method import AnalystMethodResult, LLMHandlerLike

logger = logging.getLogger(__name__)

KIND_NAME: str = "competing_hypotheses"
"""The canonical analyst-kind name. ``ach`` is accepted as an alias in the
descriptor + dispatch (see :data:`KIND_ALIASES`) for ergonomics."""

KIND_ALIASES: tuple[str, ...] = ("ach",)

HANDLER_VERSION: str = "0.1.0"
PROMPT_MODULE_PATH: str = "legba.prompts.competing_hypotheses.v1"

# OUTPUT_KIND is TRACE_ONLY: this META analyst's REAL product is the
# `hypothesis` rows it side-writes via write_hypothesis on the run's own
# connection. The per-run summary FindingPayload it returns is purely a
# run-receipt — and every run is already fully audited in `analyst_traces`
# (the summary survives in `analyst_traces.output_payload`). Marking it
# TRACE_ONLY stops the redundant FINDING row in `analyst_outputs` ("Findings as
# a real output type" cleanup) while keeping the trace + the write_hypothesis
# side-writes intact. `run_method` still returns AnalystMethodResult(
# finding=<summary>) so the trace captures the topics/confirmed/refuted counts.
from ..provenance.kinds import TRACE_ONLY as _TRACE_ONLY  # noqa: E402
from ..provenance.kinds import OutputKind as _OutputKind  # noqa: E402,F401

OUTPUT_KIND: object = _TRACE_ONLY


# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------

DEFAULT_MAX_TOKENS: int = 1024
"""Completion budget per hypothesis-generation call. The output is a small JSON
list of competing theses + counter-theses — one call per focal topic."""

DEFAULT_TEMPERATURE: float = 0.2
"""ACH hypothesis generation wants determinism (reproducible competing set)."""

MAX_TOPICS_PER_RUN: int = 12
"""Hard cap on focal topics analysed per cadence tick (bounds per-run LLM spend
regardless of how many situations exist)."""

MAX_EVIDENCE_PER_TOPIC: int = 24
"""Cap on evidence items (facts + findings + nexuses) pulled into one topic's
matrix. Keeps the matrix bounded + the prompt small."""

MIN_HYPOTHESES: int = 2
"""ACH requires AT LEAST two competing hypotheses — one is not a competition.
The deterministic fallback always yields three (escalate / de-escalate /
status-quo)."""

# evidence_balance thresholds for the auto-transitions (the old ACH ±2).
CONFIRM_K: int = 2
REFUTE_K: int = 2

# EXOGENOUS resolution (closes the circular Brier): a hypothesis is resolved
# against facts produced AFTER it, not its own evidence_balance.
try:
    RESOLUTION_MIN_AGE_DAYS: int = max(0, int(os.getenv("LEGBA_RESOLUTION_MIN_AGE_DAYS", "2")))
except (TypeError, ValueError):
    RESOLUTION_MIN_AGE_DAYS = 2
"""Minimum age before a hypothesis is auto-resolved against SUBSEQUENT facts.
The ``produced_at > hypothesis`` filter already excludes the hypothesis's own
evidence, so this floor only governs how much *lag* we require before grading.
Default 2d balances anti-circularity against this system's hours-scale fact
arrival — a generic 7d floor starved the candidate set to 0 against a ~3-day data
window (Phase-4 A-DIAG: 7d→0, 2d→50, 1d→98 candidates). Operator-tunable via
``LEGBA_RESOLUTION_MIN_AGE_DAYS`` (e.g. 1 on a fast dev stack), read at process
start. HONESTY: a low floor yields EXOGENOUS but SHORT-HORIZON resolutions — real
post-hypothesis evidence, NOT yet validated multi-month forecasting; the
calibration metric stays labelled short-horizon/self-consistency until horizon +
volume justify trust."""

RESOLUTION_MIN_SUBSEQUENT_FACTS: int = 1
"""At least this many facts produced AFTER the hypothesis must bear on its
entities before it can be auto-resolved (otherwise leave it unresolved/NULL)."""

RESOLUTION_LOOKBACK_DAYS: int = 60
"""How far back to scan for unresolved hypotheses to auto-resolve per sweep."""

MAX_RESOLUTIONS_PER_RUN: int = 200
"""Cap on hypotheses auto-resolved per sweep (bounds the resolver's DB work)."""

# Status-transition (SELF-CONSISTENCY) resolution — FIX P1-2.
#
# HONESTY: this is NOT exogenous ground truth. A hypothesis's terminal status
# (confirmed / refuted) is auto-transitioned from its OWN evidence_balance, and
# the claimed confidence the Brier scores is ALSO derived from that same
# evidence_balance. So a Brier computed over status-transition resolutions is a
# SELF-CONSISTENCY check (does the system agree with itself), NOT a calibration
# against reality. It is fed ONLY so the dormant calibration loop reports a
# numeric Brier where 0 exogenous outcomes exist — distinctly LABELLED
# `resolved_by='status_transition'` so calibration_tracking can segregate it
# from the exogenous `subsequent_facts` / `operator:*` sources and explicitly
# flag the Brier as self-consistency. The exogenous resolver
# (`_resolve_hypotheses_against_subsequent_facts`) is the REAL calibration seam
# and is preferred; this fills the gap, never overwrites an exogenous row.
ENABLE_STATUS_TRANSITION_RESOLUTION: bool = True
"""Master switch for the self-consistency resolver. Operator sets
``options['enable_status_transition_resolution']=False`` to disable it (e.g.
once enough exogenous outcomes exist that the self-consistency fill is no longer
wanted)."""

_RESOLVED_BY_STATUS_TRANSITION: str = "status_transition"
"""The distinct, SELF-CONSISTENCY resolution-source label. NEVER conflated with
the exogenous `subsequent_facts` / `operator:*` labels."""

# D16 — the `subsequent_facts` resolver grades a forward-looking ("will…") ACH
# thesis by a CHEAP LEXICAL PROXY: it scores the SUBSTRING direction (escalation
# vs de-escalation vocabulary) of subsequent facts and matches it against the
# thesis direction. That is NOT a falsifiable count over a frozen event class (as
# the acute-forecast pilot is) — it is a keyword classifier that can confirm a
# "will escalate" thesis purely because later headlines happen to contain
# escalation words. So while it touches the world (unlike status_transition), it
# is a WEAK/LEXICAL tier, not headline-exogenous. The calibration handler keys on
# this exact label (`calibration_tracking._WEAK_LEXICAL_SOURCES`) to DEMOTE these
# resolutions out of the headline exogenous Brier. The producer keeps the
# existing ABSTAIN on undirected theses (below) so it never even MINTS a
# lexically-graded outcome it cannot direction-classify.
_RESOLVED_BY_SUBSEQUENT_FACTS: str = "subsequent_facts"
"""WEAK / LEXICAL resolution-source label (D16). World-touching but graded by a
substring direction proxy — DEMOTED out of the headline exogenous Brier by
``calibration_tracking._WEAK_LEXICAL_SOURCES``. Distinct from the falsifiable
`forecast_acute_exogenous` / `forecast_vs_actual` / `operator:*` sources."""

# Consistency scale (Heuer's ACH notation mapped to integers). The LLM (or the
# deterministic scorer) labels each (evidence, hypothesis) cell with one of these.
_CONSISTENCY_SCALE: dict[str, int] = {
    "CC": 2,   # strongly consistent
    "C": 1,    # consistent
    "N": 0,    # neutral / not applicable
    "I": -1,   # inconsistent
    "II": -2,  # strongly inconsistent
}
_CONSISTENCY_BY_INT: dict[int, str] = {v: k for k, v in _CONSISTENCY_SCALE.items()}


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ---------------------------------------------------------------------------
# Deps surface — LLM port + pg_pool + budget (the relationship_reifier shape).
# This kind BOTH calls the LLM (hypothesis generation) AND reads/writes the
# substrate directly (evidence base + side-written HYPOTHESIS rows).
# ---------------------------------------------------------------------------


@runtime_checkable
class _BudgetLike(Protocol):
    async def check_envelope(self) -> str: ...


@dataclass
class ACHDeps:
    """The dep bundle ``run_method`` needs.

    Built by ``analyst_deps_builder._build_competing_hypotheses`` from the
    resolved primary LLM + the run's ``StandardDeps`` (pg_pool + budget). Tests
    construct it directly with a stub LLM + a real test pg_pool.
    """

    llm: LLMHandlerLike | None = None
    pg_pool: Any = None
    budget: _BudgetLike | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    max_topics: int = MAX_TOPICS_PER_RUN
    system_prompt: str | None = None


# ---------------------------------------------------------------------------
# Hypothesis-generation prompt
# ---------------------------------------------------------------------------

from ._tradecraft import with_preamble  # noqa: E402

_SYSTEM_PROMPT = with_preamble(
    """TASK — ANALYSIS OF COMPETING HYPOTHESES (ACH) over a focal topic. Given the topic and its current evidence base, produce a SET of MUTUALLY-COMPETING hypotheses that could each explain or predict the topic's trajectory. Rules you MUST follow:
  * Produce AT LEAST TWO competing hypotheses (a single hypothesis is not a competition).
  * They must be MUTUALLY EXCLUSIVE alternatives, not variations of one claim.
  * Cover the space: include a status-quo / "no material change" hypothesis when plausible, so the set is collectively as exhaustive as the evidence allows.
  * EVERY hypothesis carries a MANDATORY counter-thesis (the strongest case AGAINST it) — confirmation bias is forbidden.
Return ONE JSON object, nothing else:
{
  "hypotheses": [
    {"thesis": "<a competing explanation/forecast>", "counter_thesis": "<the strongest case against this thesis>"},
    ... (>= 2 entries, mutually exclusive)
  ]
}
Make the hypotheses genuinely DIVERGENT so the evidence can discriminate between them; prefer theses whose truth would be settled by DIFFERENT evidence."""
)


def build_prompt_module() -> Any:
    """Construct the DSPy module bound to this kind (GEPA optimization twin).

    Lazy-imports so this file imports cleanly when dspy isn't installed; raises
    :class:`ModuleNotFoundError` otherwise, matching the sibling kinds' contract.
    Not on the runtime hot path — the kind calls ``chat_complete`` directly.
    """
    from legba.prompts.competing_hypotheses.v1 import build as _build

    return _build()


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Pull the first balanced ``{...}`` object out of an LLM response (handles
    ```json fences + leading prose). Mirrors the sibling kinds' parser. Returns
    None on failure (degrade)."""
    if not raw:
        return None
    candidate = raw.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    start = candidate.find("{")
    if start < 0:
        return None
    depth = 0
    end = -1
    for i in range(start, len(candidate)):
        c = candidate[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return None
    try:
        parsed = json.loads(candidate[start:end])
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# Evidence-base reads — temporally-CURRENT (the Piece B / Piece A inputs)
# ---------------------------------------------------------------------------


async def _read_focal_topics(conn: Any, *, limit: int, lookback_days: int) -> list[dict[str, Any]]:
    """Pull the focal topics for this sweep — the recent situations, ranked by
    intensity. ACH is NOT gated on ``status='active'`` (that gate is exactly
    what starved the old ``hypothesis_lifecycle``); any situation with recent
    activity is a candidate topic.
    """
    rows = await conn.fetch(
        f"""
        SELECT id, name, category, status, intensity_score, derived_from
          FROM situations
         WHERE last_event_at > NOW() - make_interval(days => {int(lookback_days)})
         ORDER BY intensity_score DESC, last_event_at DESC
         LIMIT {int(limit)}
        """,
    )
    return [dict(r) for r in rows]


async def _resolve_topic_entities(conn: Any, *, name: str) -> list[str]:
    """Resolve a focal-topic name to its canonical ENTITY SET.

    The evidence scope is the topic's resolved entities, NOT a raw ``LIKE
    '%name%'`` substring (which false-matches — e.g. "Iran" hitting
    "Iranian-American Society of Ohio", or "Georgia" the country hitting Georgia
    the US state). We resolve the name through ``entity_profiles`` — the
    composite-key ``(lower(canonical_name), entity_class)`` dedup table (migration
    0035) that already distinguishes a country from a location from a person — and
    return the set of CANONICAL NAMES the topic maps to. Facts/nexuses are then
    scoped by exact membership in that set.

    The topic name itself is always included (lower-cased) so a topic whose
    entities haven't been materialised yet still scopes to its own exact name
    rather than degrading back to a loose substring. Returns a de-duplicated,
    lower-cased list.
    """
    resolved: list[str] = []
    seen: set[str] = set()

    def _add(n: str) -> None:
        key = n.strip().lower()
        if key and key not in seen:
            seen.add(key)
            resolved.append(key)

    if not name:
        return resolved
    _add(name)
    try:
        rows = await conn.fetch(
            """
            SELECT canonical_name
              FROM entity_profiles
             WHERE lower(canonical_name) = lower($1)
                OR lower($1) LIKE '%' || lower(canonical_name) || '%'
             ORDER BY completeness_score DESC
             LIMIT 16
            """,
            name,
        )
    except Exception as exc:  # pragma: no cover - best-effort resolution
        logger.debug("competing_hypotheses.entity_resolve_failed name=%s err=%s", name, exc)
        return resolved
    for r in rows:
        _add(str(r["canonical_name"] or ""))
    return resolved


async def _read_evidence_for_topic(
    conn: Any, *, situation: Mapping[str, Any], limit: int
) -> list[dict[str, Any]]:
    """Assemble the temporally-CURRENT evidence base for one focal topic.

    Three sources, all interchangeable as ``{id, kind, text, polarity, ...}``
    evidence items:

      * **facts** — ``superseded_by IS NULL AND valid_until IS NULL`` (the
        "what is true now" open-row query Piece B made meaningful), scoped to the
        topic's RESOLVED entity set (``entity_profiles`` canonical names — exact
        membership, NOT a ``LIKE '%name%'`` substring).
      * **findings** — ``analyst_outputs`` rows (``kind='finding'``) linked to the
        situation via ``situations.derived_from``.
      * **nexuses** — open signed/typed relationships (Piece A) whose subject or
        object is in the topic's resolved entity set.

    The ``id`` of each item is a real substrate UUID so it can land in
    ``supporting_signals`` / ``refuting_signals`` / ``derived_from`` (lineage).
    """
    sit_id = situation["id"]
    name = str(situation.get("name") or "")
    derived = list(situation.get("derived_from") or [])
    evidence: list[dict[str, Any]] = []

    # Resolve the topic to its entity set ONCE — facts + nexuses both scope to it.
    entity_names = await _resolve_topic_entities(conn, name=name)

    # 1) Findings linked to the situation (forward-claim-honest: the lineage
    #    edge is the situation's own derived_from).
    if derived:
        find_rows = await conn.fetch(
            """
            SELECT id, title, body, confidence, produced_at
              FROM analyst_outputs
             WHERE kind = 'finding' AND id = ANY($1::uuid[])
             ORDER BY produced_at DESC
             LIMIT $2
            """,
            derived, limit,
        )
        for r in find_rows:
            evidence.append({
                "id": r["id"],
                "kind": "finding",
                "text": (str(r["title"] or "") + " " + str(r["body"] or ""))[:600],
                "confidence": float(r["confidence"] or 0.5),
                "produced_at": r["produced_at"],
                "polarity": 0,
            })

    # 2) Current facts (open-row query — Piece B's temporally-honest read),
    #    scoped to the topic's RESOLVED entity set by EXACT subject membership
    #    (lower(subject) = ANY(entity_names)) — not a loose substring.
    remaining = max(0, limit - len(evidence))
    if remaining and entity_names:
        fact_rows = await conn.fetch(
            """
            SELECT id, subject, predicate, value, confidence, produced_at
              FROM facts
             WHERE superseded_by IS NULL AND valid_until IS NULL
               AND lower(subject) = ANY($1::text[])
             ORDER BY confidence DESC, produced_at DESC
             LIMIT $2
            """,
            entity_names, remaining,
        )
        for r in fact_rows:
            evidence.append({
                "id": r["id"],
                "kind": "fact",
                "text": f"{r['subject']} {r['predicate']} {r['value']}"[:600],
                "confidence": float(r["confidence"] or 0.5),
                "produced_at": r["produced_at"],
                "polarity": 0,
            })

    # 3) Open signed/typed nexuses (Piece A) whose subject OR object is in the
    #    topic's resolved entity set (exact membership, not substring).
    remaining = max(0, limit - len(evidence))
    if remaining and entity_names:
        nexus_rows = await conn.fetch(
            """
            SELECT id, subject, object, rel_type, polarity, intent, confidence,
                   produced_at
              FROM nexuses
             WHERE valid_until IS NULL AND superseded_by IS NULL
               AND (lower(subject) = ANY($1::text[])
                    OR lower(object)  = ANY($1::text[]))
             ORDER BY confidence DESC, produced_at DESC
             LIMIT $2
            """,
            entity_names, remaining,
        )
        for r in nexus_rows:
            evidence.append({
                "id": r["id"],
                "kind": "nexus",
                "text": f"{r['subject']} {r['rel_type']} {r['object']} "
                        f"(intent={r['intent']})"[:600],
                "confidence": float(r["confidence"] or 0.5),
                "produced_at": r["produced_at"],
                "polarity": int(r["polarity"] or 0),
            })

    # 4) Contested-fact-value diagnostics (Holes-B Wave 5, #101). An OPEN
    #    contention group whose subject is one of the topic's resolved entities
    #    is a FIRST-CLASS ACH diagnostic: the substrate itself is telling us
    #    credible sources DISAGREE on a (subject, predicate) value. We surface
    #    the dispute as one ``contested_fact_value`` item per group, listing the
    #    competing non-junk values + the arbiter's surfaced winner (if any) —
    #    so the matrix can reason FROM the disagreement (which value a
    #    hypothesis implies) instead of silently picking one side. Read-only:
    #    we never close, surface, or mutate a fact or a group here.
    remaining = max(0, limit - len(evidence))
    if remaining and entity_names:
        contention_rows = await conn.fetch(
            """
            SELECT fc.id,
                   fc.subject_key,
                   fc.predicate_key,
                   fc.status,
                   fc.surfaced_value,
                   fc.value_count,
                   fc.updated_at,
                   COALESCE(
                       array_agg(fcv.value_key ORDER BY fcv.arbiter_score DESC NULLS LAST)
                         FILTER (WHERE fcv.is_junk = false),
                       '{}'
                   ) AS competing_values
              FROM fact_contention fc
              LEFT JOIN fact_contention_values fcv ON fcv.contention_id = fc.id
             WHERE fc.status IN ('contested', 'surfaced')
               AND fc.subject_key = ANY($1::text[])
             GROUP BY fc.id, fc.subject_key, fc.predicate_key, fc.status,
                      fc.surfaced_value, fc.value_count, fc.updated_at
             ORDER BY fc.value_count DESC, fc.updated_at DESC
             LIMIT $2
            """,
            entity_names, remaining,
        )
        for r in contention_rows:
            values = [str(v) for v in (r["competing_values"] or []) if v]
            surfaced = r["surfaced_value"]
            disagree = " vs ".join(values) if values else "(values unavailable)"
            winner = (
                f"surfaced winner='{surfaced}'" if surfaced
                else "NO surfaced winner (arbiter abstained — unresolved)"
            )
            text = (
                f"CONTESTED CLAIM: sources disagree on "
                f"'{r['subject_key']}' {r['predicate_key']}: {disagree}; {winner}"
            )
            evidence.append({
                "id": r["id"],
                "kind": "contested_fact_value",
                "text": text[:600],
                # The dispute is maximally DIAGNOSTIC (it is the platform's own
                # flag of an unresolved disagreement), but it does not assert a
                # value — polarity is neutral so it doesn't pre-bias a side.
                "confidence": 0.9,
                "produced_at": r["updated_at"],
                "polarity": 0,
            })

    logger.debug(
        "competing_hypotheses.evidence topic=%s items=%d", sit_id, len(evidence),
    )
    return evidence[:limit]


# ---------------------------------------------------------------------------
# Hypothesis generation — LLM (enrichment) with a deterministic fallback
# ---------------------------------------------------------------------------


def _deterministic_hypotheses(topic_name: str) -> list[dict[str, str]]:
    """The degrade-not-drop fallback hypothesis set: three mutually-exclusive
    trajectories, each with a mandatory counter-thesis. Used when no LLM is
    available, the budget is exhausted, or the LLM output is unusable — so the
    ACH matrix + balance + rows ALWAYS get built."""
    label = (topic_name or "this situation").strip() or "this situation"
    return [
        {
            "thesis": f"{label} will escalate over the next 14 days",
            "counter_thesis": f"{label} de-escalates or holds — the escalation "
                              "drivers are transient",
        },
        {
            "thesis": f"{label} will de-escalate over the next 14 days",
            "counter_thesis": f"{label} keeps escalating — de-escalation pressure "
                              "is insufficient",
        },
        {
            "thesis": f"{label} will remain at its current intensity (status quo)",
            "counter_thesis": f"{label} breaks the status quo — a tipping factor "
                              "is already present",
        },
    ]


def _build_generation_prompt(
    *, topic_name: str, category: str, evidence: Sequence[Mapping[str, Any]]
) -> str:
    lines = [
        f"FOCAL TOPIC: {topic_name}",
        f"Category: {category or '(uncategorised)'}",
        "",
        "CURRENT EVIDENCE BASE (temporally-current facts, linked findings, signed "
        "relationships):",
    ]
    for i, e in enumerate(evidence, start=1):
        lines.append(f"  [{i}] ({e.get('kind')}) {str(e.get('text'))[:240]}")
    if not evidence:
        lines.append("  (no current evidence — reason from the topic alone)")
    lines.append("")
    lines.append(
        "Produce the competing-hypothesis SET as the JSON object specified "
        "(>= 2 mutually-exclusive hypotheses, each with a mandatory counter_thesis)."
    )
    return "\n".join(lines)


def _coerce_hypotheses(obj: Mapping[str, Any], *, topic_name: str) -> list[dict[str, str]]:
    """Coerce the LLM object into a clean hypothesis list. Enforces the ACH
    invariants: >= MIN_HYPOTHESES entries, every entry carries a non-empty thesis
    AND a non-empty counter-thesis. Returns ``[]`` when the shape is unusable so
    the caller falls back to the deterministic set."""
    raw = obj.get("hypotheses")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        thesis = str(entry.get("thesis") or "").strip()
        counter = str(entry.get("counter_thesis") or entry.get("counter") or "").strip()
        if not thesis:
            continue
        key = thesis.lower()
        if key in seen:
            continue
        seen.add(key)
        if not counter:
            # Mandatory counter-thesis: synthesize the negation rather than drop
            # the hypothesis (keep the competing set wide), but never leave it
            # empty — the ACH invariant.
            counter = f"It is not the case that {thesis}"
        out.append({"thesis": thesis[:4096], "counter_thesis": counter[:4096]})
    if len(out) < MIN_HYPOTHESES:
        return []
    return out


async def _generate_hypotheses(
    deps: ACHDeps,
    *,
    topic_name: str,
    category: str,
    evidence: Sequence[Mapping[str, Any]],
    system_prompt: str,
) -> tuple[list[dict[str, str]], dict[str, int], bool]:
    """Generate the competing-hypothesis set. Returns ``(hypotheses, usage,
    degraded)``. degrade-not-drop: any LLM/parse failure or budget pause falls
    back to the deterministic triad and flips ``degraded``."""
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    if deps.llm is None:
        return _deterministic_hypotheses(topic_name), usage, True
    # Budget gate BEFORE the call (degrade-not-drop: fall back, don't raise).
    if deps.budget is not None:
        try:
            envelope = await deps.budget.check_envelope()
        except Exception:  # pragma: no cover - defensive
            envelope = "ok"
        if envelope != "ok":
            return _deterministic_hypotheses(topic_name), usage, True

    user_prompt = _build_generation_prompt(
        topic_name=topic_name, category=category, evidence=evidence,
    )
    try:
        response = await deps.llm.chat_complete(
            [{"role": "user", "content": user_prompt}],
            max_tokens=deps.max_tokens,
            temperature=deps.temperature,
            system=system_prompt,
        )
    except Exception as exc:
        logger.warning("competing_hypotheses.llm_failed topic=%s err=%s", topic_name, exc)
        return _deterministic_hypotheses(topic_name), usage, True

    content = getattr(response, "content", "") or ""
    usage_raw = getattr(response, "usage", None)
    if usage_raw is not None:
        usage = {
            "prompt_tokens": int(getattr(usage_raw, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage_raw, "completion_tokens", 0) or 0),
            "reasoning_tokens": int(getattr(usage_raw, "reasoning_tokens", 0) or 0),
        }
    obj = _extract_json_object(content)
    if obj is None:
        logger.warning("competing_hypotheses.parse_failed topic=%s", topic_name)
        return _deterministic_hypotheses(topic_name), usage, True
    hyps = _coerce_hypotheses(obj, topic_name=topic_name)
    if not hyps:
        return _deterministic_hypotheses(topic_name), usage, True
    return hyps, usage, False


# ---------------------------------------------------------------------------
# The ACH matrix — consistency scoring + DIAGNOSTICITY weighting
# ---------------------------------------------------------------------------


# Directional lexical cue sets — shared by the consistency scorer AND the
# exogenous resolver's thesis classifier so the two can NEVER drift (DQ-H2b).
# De-escalation is tested BEFORE escalation because "de-escalate" contains
# "escalate" (the substring trap).
_DEESC_CUES = (
    "de-escalate", "deescalate", "de-escalat", "resolve", "stabil", "ease",
    "calm", "subside",
)
_ESC_CUES = ("escalate", "escalation", "intensif", "worsen", "rise")
_STATUS_QUO_CUES = ("status quo", "remain", "unchanged", "hold", "current intensity")


def _score_consistency(
    evidence_text: str,
    evidence_polarity: int,
    thesis: str,
) -> int:
    """Deterministically score one (evidence, hypothesis) cell on the
    :data:`_CONSISTENCY_SCALE` (-2..+2).

    This is a transparent lexical scorer (NO LLM in the matrix — the LLM only
    proposes the hypothesis SET; the matrix is reproducible). It keys off:

      * directional intent of the thesis (escalation vs de-escalation / stability),
      * directional polarity of the evidence (a hostile/-1 nexus is escalation
        evidence; a supportive/+1 nexus is de-escalation evidence),
      * escalation/de-escalation lexical cues in the evidence text.

    The exact numbers matter less than the SPREAD across hypotheses — that spread
    is what :func:`_diagnosticity` turns into evidence weight (the ACH core).
    """
    t = thesis.lower()
    text = evidence_text.lower()

    # NOTE order matters: "de-escalate" CONTAINS "escalate", so test the
    # de-escalation cues FIRST and gate escalation on their absence — otherwise a
    # de-escalation thesis would mis-classify as escalation (the substring trap).
    thesis_deescalates = any(w in t for w in _DEESC_CUES)
    thesis_escalates = (not thesis_deescalates) and any(w in t for w in _ESC_CUES)
    thesis_status_quo = any(w in t for w in _STATUS_QUO_CUES)

    esc_cues = sum(
        text.count(w) for w in ("attack", "strike", "escalat", "killed", "clash",
                                "hostil", "sanction", "threat", "deploy", "missile",
                                "conflict", "war", "invad", "raid")
    )
    deesc_cues = sum(
        text.count(w) for w in ("ceasefire", "truce", "talks", "agreement", "deal",
                                "withdraw", "peace", "de-escalat", "resolv", "ally",
                                "alli", "support", "cooper")
    )

    # Polarity of a signed nexus is a strong directional cue.
    if evidence_polarity < 0:
        esc_cues += 2
    elif evidence_polarity > 0:
        deesc_cues += 2

    net = esc_cues - deesc_cues  # >0 => escalation-leaning evidence

    if thesis_escalates:
        score = net
    elif thesis_deescalates:
        score = -net
    elif thesis_status_quo:
        # Status quo is consistent with WEAK directional evidence, inconsistent
        # with strong moves either way.
        score = 1 if abs(net) == 0 else -abs(net)
    else:
        # Undirected thesis — fall back to mild consistency with any evidence.
        score = 0

    # Clamp to the {-2..+2} scale.
    if score >= 2:
        return 2
    if score == 1:
        return 1
    if score == 0:
        return 0
    if score == -1:
        return -1
    return -2


# --- LLM-per-cell consistency scorer (enrichment over the lexical scorer) ----
#
# The lexical _score_consistency above is reproducible but SHALLOW — it keys off
# directional keyword cues, so it cannot read an item that is consistent for
# REASONS other than escalation/de-escalation vocabulary (the "lexical/self-
# consistency" caveat in the honesty disclosure). When a budget envelope is
# available we score every (evidence, hypothesis) cell with the LLM on Heuer's
# CC/C/N/I/II scale in ONE batched call, then fall back PER CELL to the lexical
# scorer for any cell the LLM didn't return. The LLM is reached only through the
# analyst provider plane (deps.llm.chat_complete) — never litellm/dspy.

_CONSISTENCY_SYSTEM_PROMPT = (
    "You are an intelligence analyst filling in an ANALYSIS OF COMPETING "
    "HYPOTHESES (ACH) consistency matrix. You are given a list of competing "
    "HYPOTHESES and a list of EVIDENCE items. For EACH (evidence, hypothesis) "
    "pair you judge how CONSISTENT the evidence is with the hypothesis being "
    "TRUE, using Heuer's five-point scale:\n"
    "  CC = strongly consistent (the evidence is much more likely if the "
    "hypothesis is true)\n"
    "  C  = consistent (somewhat more likely if true)\n"
    "  N  = neutral / not applicable (the evidence does not discriminate)\n"
    "  I  = inconsistent (somewhat less likely if true)\n"
    "  II = strongly inconsistent (much less likely if true)\n"
    "Judge each cell INDEPENDENTLY on the evidence's bearing on THAT hypothesis "
    "— do not anchor on a hypothesis you think is correct. Return ONE JSON "
    "object and nothing else:\n"
    '{"cells": [{"e": <evidence_index>, "h": <hypothesis_index>, '
    '"label": "CC|C|N|I|II"}, ...]}\n'
    "Include one entry for every (evidence_index, hypothesis_index) pair."
)


def _build_consistency_prompt(
    *,
    hypotheses: Sequence[Mapping[str, str]],
    evidence: Sequence[Mapping[str, Any]],
) -> str:
    lines = ["HYPOTHESES:"]
    for h_idx, h in enumerate(hypotheses):
        lines.append(f"  [h{h_idx}] {str(h.get('thesis') or '')[:240]}")
    lines.append("")
    lines.append("EVIDENCE:")
    for e_idx, e in enumerate(evidence):
        lines.append(f"  [e{e_idx}] ({e.get('kind')}) {str(e.get('text') or '')[:240]}")
    lines.append("")
    lines.append(
        "Score EVERY (evidence_index, hypothesis_index) cell on the CC/C/N/I/II "
        "scale and return the JSON object specified."
    )
    return "\n".join(lines)


def _coerce_cell_scores(
    obj: Mapping[str, Any], *, n_evidence: int, n_hyp: int
) -> dict[tuple[int, int], int]:
    """Coerce the LLM cell list into ``{(e_idx, h_idx): consistency_int}`` on the
    -2..+2 scale. Out-of-range indices and unknown labels are dropped (the caller
    fills any missing cell from the lexical scorer — degrade-not-drop per cell)."""
    raw = obj.get("cells")
    if not isinstance(raw, list):
        return {}
    out: dict[tuple[int, int], int] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        try:
            e_idx = int(entry.get("e"))
            h_idx = int(entry.get("h"))
        except (TypeError, ValueError):
            continue
        if not (0 <= e_idx < n_evidence and 0 <= h_idx < n_hyp):
            continue
        label = str(entry.get("label") or "").strip().upper()
        if label not in _CONSISTENCY_SCALE:
            continue
        out[(e_idx, h_idx)] = _CONSISTENCY_SCALE[label]
    return out


async def _score_consistency_matrix_llm(
    deps: "ACHDeps",
    *,
    hypotheses: Sequence[Mapping[str, str]],
    evidence: Sequence[Mapping[str, Any]],
) -> tuple[dict[tuple[int, int], int] | None, dict[str, int]]:
    """Score the full ACH matrix with the LLM in ONE batched call.

    Returns ``(cell_scores, usage)`` where ``cell_scores`` maps
    ``(evidence_index, hypothesis_index) -> consistency int`` on the -2..+2
    scale, or ``None`` when the LLM is unavailable, the budget envelope is not
    ``ok``, the call raises, or the output is unparsable — in every such case the
    caller scores deterministically with the lexical :func:`_score_consistency`
    (the budget-exhausted FALLBACK). Partial coverage is fine: any cell the LLM
    omits is filled lexically by :func:`build_ach_matrix`.
    """
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    if deps.llm is None or not hypotheses or not evidence:
        return None, usage
    if deps.budget is not None:
        try:
            envelope = await deps.budget.check_envelope()
        except Exception:  # pragma: no cover - defensive
            envelope = "ok"
        if envelope != "ok":
            return None, usage

    user_prompt = _build_consistency_prompt(hypotheses=hypotheses, evidence=evidence)
    try:
        response = await deps.llm.chat_complete(
            [{"role": "user", "content": user_prompt}],
            max_tokens=deps.max_tokens,
            temperature=0.0,  # the matrix wants the most-deterministic call
            system=_CONSISTENCY_SYSTEM_PROMPT,
        )
    except Exception as exc:
        logger.warning("competing_hypotheses.matrix_llm_failed err=%s", exc)
        return None, usage

    usage_raw = getattr(response, "usage", None)
    if usage_raw is not None:
        usage = {
            "prompt_tokens": int(getattr(usage_raw, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage_raw, "completion_tokens", 0) or 0),
            "reasoning_tokens": int(getattr(usage_raw, "reasoning_tokens", 0) or 0),
        }
    obj = _extract_json_object(getattr(response, "content", "") or "")
    if obj is None:
        logger.warning("competing_hypotheses.matrix_parse_failed")
        return None, usage
    cells = _coerce_cell_scores(obj, n_evidence=len(evidence), n_hyp=len(hypotheses))
    if not cells:
        return None, usage
    return cells, usage


def _diagnosticity(consistency_row: Sequence[int]) -> float:
    """Diagnosticity of one evidence item = how much it DISCRIMINATES between the
    hypotheses.

    The ACH insight: evidence consistent with EVERY hypothesis tells you nothing
    (zero diagnostic value); evidence that fits some hypotheses and contradicts
    others is what moves your conclusion. We measure discrimination as the SPREAD
    (max - min) of the item's consistency scores across the competing hypotheses,
    normalised to ``[0, 1]`` over the scale's full range (4 = +2..-2). An item
    with identical scores across all hypotheses has spread 0 => weight 0.
    """
    if not consistency_row:
        return 0.0
    spread = max(consistency_row) - min(consistency_row)
    return round(spread / 4.0, 4)  # 4 = the {-2..+2} range


def build_ach_matrix(
    hypotheses: Sequence[Mapping[str, str]],
    evidence: Sequence[Mapping[str, Any]],
    *,
    cell_scores: Mapping[tuple[int, int], int] | None = None,
) -> dict[str, Any]:
    """Build the full ACH evidence x hypothesis matrix + diagnosticity + the
    diagnosticity-weighted integer evidence balance per hypothesis.

    ``cell_scores`` optionally supplies precomputed ``(evidence_index,
    hypothesis_index) -> consistency`` values from the LLM matrix scorer
    (:func:`_score_consistency_matrix_llm`). When a cell is present it OVERRIDES
    the lexical scorer; any cell absent from the map (or when ``cell_scores`` is
    ``None``) is scored by the deterministic lexical :func:`_score_consistency`
    — so the matrix is identical whether the LLM is enriching it or exhausted.

    Returns a dict carrying:

      * ``hypotheses`` — ``[{thesis, counter_thesis}]`` (echoed for the payload).
      * ``evidence``   — ``[{id, kind, text, diagnosticity}]`` per item.
      * ``matrix``     — list of rows; row ``i`` = ``{evidence_index, cells:
        [{hypothesis_index, consistency, label}], diagnosticity}``.
      * ``balance``    — ``[int]`` parallel to ``hypotheses``: the
        diagnosticity-weighted, sign-of-consistency integer balance.
      * ``lead_index`` / ``refuted_indices`` — the auto-transition decisions.

    The balance is an INTEGER (the old ACH invariant — robust to confidence
    gaming): for each hypothesis we sum the SIGN of each evidence cell's
    consistency, but only count cells whose item is DIAGNOSTIC (diagnosticity >
    0). Non-diagnostic evidence contributes nothing — the ACH core.
    """
    n_hyp = len(hypotheses)
    matrix_rows: list[dict[str, Any]] = []
    # raw weighted contributions accumulate as floats, rounded to int at the end
    weighted: list[float] = [0.0 for _ in range(n_hyp)]
    ev_out: list[dict[str, Any]] = []

    for ev_idx, ev in enumerate(evidence):
        ev_text = str(ev.get("text") or "")
        ev_pol = int(ev.get("polarity") or 0)
        row_scores = []
        for h_idx, h in enumerate(hypotheses):
            # LLM cell value when present (enrichment); lexical otherwise/per-gap.
            if cell_scores is not None and (ev_idx, h_idx) in cell_scores:
                row_scores.append(int(cell_scores[(ev_idx, h_idx)]))
            else:
                row_scores.append(
                    _score_consistency(ev_text, ev_pol, str(h.get("thesis") or ""))
                )
        diag = _diagnosticity(row_scores)
        cells = [
            {
                "hypothesis_index": h_idx,
                "consistency": row_scores[h_idx],
                "label": _CONSISTENCY_BY_INT.get(row_scores[h_idx], "N"),
            }
            for h_idx in range(n_hyp)
        ]
        matrix_rows.append({
            "evidence_index": ev_idx,
            "evidence_id": str(ev.get("id")) if ev.get("id") is not None else None,
            "diagnosticity": diag,
            "cells": cells,
        })
        ev_out.append({
            "id": str(ev.get("id")) if ev.get("id") is not None else None,
            "kind": ev.get("kind"),
            "text": str(ev.get("text") or "")[:240],
            "diagnosticity": diag,
        })
        # Diagnosticity-weighted sign accumulation.
        if diag > 0.0:
            for h_idx in range(n_hyp):
                s = row_scores[h_idx]
                if s > 0:
                    weighted[h_idx] += diag
                elif s < 0:
                    weighted[h_idx] -= diag

    balance = [int(round(w)) for w in weighted]

    lead_index = max(range(n_hyp), key=lambda i: balance[i]) if n_hyp else None
    refuted_indices = [i for i in range(n_hyp) if balance[i] <= -REFUTE_K]

    return {
        "hypotheses": [
            {"thesis": h.get("thesis"), "counter_thesis": h.get("counter_thesis")}
            for h in hypotheses
        ],
        "evidence": ev_out,
        "matrix": matrix_rows,
        "balance": balance,
        "lead_index": lead_index,
        "refuted_indices": refuted_indices,
        "consistency_scale": _CONSISTENCY_SCALE,
    }


def _status_for(*, hypothesis_index: int, ach: Mapping[str, Any]) -> str:
    """Auto-transition the hypothesis past ±K (the old ACH invariant):

      * the LEAD hypothesis whose balance >= +CONFIRM_K -> ``confirmed``;
      * any hypothesis whose balance <= -REFUTE_K -> ``refuted``;
      * everything else stays ``active``.
    """
    balance = ach.get("balance") or []
    if hypothesis_index >= len(balance):
        return "active"
    bal = int(balance[hypothesis_index])
    if hypothesis_index == ach.get("lead_index") and bal >= CONFIRM_K:
        return "confirmed"
    if bal <= -REFUTE_K:
        return "refuted"
    return "active"


# ---------------------------------------------------------------------------
# EXOGENOUS resolution — grade a hypothesis against SUBSEQUENT facts
# ---------------------------------------------------------------------------


def _thesis_direction(thesis: str) -> int:
    """Coarse directional sign of a thesis: +1 escalation, -1 de-escalation,
    0 status-quo/undirected. Reuses the same de-escalation-before-escalation
    substring discipline as :func:`_score_consistency`."""
    t = thesis.lower()
    if any(w in t for w in _DEESC_CUES):
        return -1
    if any(w in t for w in _ESC_CUES):
        return 1
    return 0


def _thesis_is_status_quo(thesis: str) -> bool:
    """True when a non-directional thesis nonetheless makes a GRADEABLE
    status-quo claim ("remains at its current intensity", "unchanged", …).

    DQ-H2b distinguishes this from a genuinely UNDIRECTED thesis (no escalation,
    de-escalation, OR status-quo content): a status-quo claim CAN be graded
    against subsequent facts (true when they stay quiet), whereas an undirected
    thesis cannot — auto-grading the latter TRUE inflated the exogenous
    true-rate, so the resolver ABSTAINS on it instead."""
    return any(w in thesis.lower() for w in _STATUS_QUO_CUES)


async def _resolve_hypotheses_against_subsequent_facts(
    conn: Any, *, now: datetime
) -> tuple[int, int]:
    """Auto-resolve unresolved hypotheses against facts produced AFTER them.

    D16 — this is the WEAK/LEXICAL half of the calibration loop, NOT a falsifiable
    exogenous grading: an unresolved hypothesis
    (``resolved_outcome IS NULL``) that is old enough (``RESOLUTION_MIN_AGE_DAYS``)
    is graded against the CURRENT facts about its entities that were produced
    SINCE the hypothesis was made — brand-new evidence the hypothesis was NOT
    built from. We read the thesis's direction (escalate / de-escalate / status
    quo) and the net direction of the subsequent facts; if they AGREE the thesis
    came true (``resolved_outcome = 1``), if they oppose it did not
    (``resolved_outcome = 0``). Status-quo / undirected theses resolve as TRUE
    when the subsequent facts are directionally quiet, FALSE when they move
    strongly. Stamped with ``resolved_by = _RESOLVED_BY_SUBSEQUENT_FACTS``
    (``'subsequent_facts'``, the WEAK/LEXICAL tier — demoted out of the headline
    exogenous Brier by ``calibration_tracking._WEAK_LEXICAL_SOURCES``) and
    ``resolved_at = now``. An OPERATOR may instead stamp ``resolved_outcome``
    directly (``resolved_by = 'operator:<id>'``, a HEADLINE-exogenous source) —
    this resolver never overwrites an already-resolved row (the
    ``resolved_outcome IS NULL`` filter).

    Returns ``(resolved_count, scanned_count)``.
    """
    try:
        candidates = await conn.fetch(
            """
            SELECT id, thesis, situation_id, produced_at
              FROM hypotheses
             WHERE resolved_outcome IS NULL
               AND produced_at < $1::timestamptz - make_interval(days => $2)
               AND produced_at > $1::timestamptz - make_interval(days => $3)
             ORDER BY produced_at ASC
             LIMIT $4
            """,
            now, RESOLUTION_MIN_AGE_DAYS, RESOLUTION_LOOKBACK_DAYS,
            MAX_RESOLUTIONS_PER_RUN,
        )
    except Exception as exc:
        logger.warning("competing_hypotheses.resolve_scan_failed err=%s", exc)
        return 0, 0

    # A-T3 diagnostics: make the exogenous-resolver gates observable. The dominant
    # zero-match cause has historically been the AGE-FLOOR gate starving the
    # candidate set (RESOLUTION_MIN_AGE_DAYS vs a younger data window), so when 0
    # candidates pass we probe how many unresolved hypotheses exist WITHIN the
    # lookback but YOUNGER than the floor — a non-zero `age_floor_blocked` with 0
    # candidates is the calendar-time-gated signature.
    if not candidates:
        try:
            age_floor_blocked = int(
                await conn.fetchval(
                    """
                    SELECT count(*) FROM hypotheses
                     WHERE resolved_outcome IS NULL
                       AND produced_at >= $1::timestamptz - make_interval(days => $2)
                       AND produced_at > $1::timestamptz - make_interval(days => $3)
                    """,
                    now, RESOLUTION_MIN_AGE_DAYS, RESOLUTION_LOOKBACK_DAYS,
                )
                or 0
            )
        except Exception:  # pragma: no cover - best-effort diagnostic
            age_floor_blocked = -1
        logger.info(
            "competing_hypotheses.resolve_no_candidates "
            "min_age_days=%s lookback_days=%s age_floor_blocked=%s",
            RESOLUTION_MIN_AGE_DAYS, RESOLUTION_LOOKBACK_DAYS, age_floor_blocked,
        )
        return 0, 0

    # A-T3 per-gate skip counters (logged once at the end of the sweep).
    skip_no_situation = 0
    skip_no_entities = 0
    skip_no_subsequent_facts = 0
    # DQ-H2b — theses with no gradeable direction (not escalation / de-escalation
    # / status-quo) are ABSTAINED, not auto-graded; track for the true-rate.
    skip_undirected = 0

    resolved = 0
    resolved_true = 0
    for hyp in candidates:
        thesis = str(hyp["thesis"] or "")
        produced_at = hyp["produced_at"]
        # Resolve the hypothesis's own situation to its entity set so subsequent
        # facts are scoped the SAME way the evidence base was.
        sit_name = ""
        if hyp["situation_id"] is not None:
            try:
                sit_name = str(
                    await conn.fetchval(
                        "SELECT name FROM situations WHERE id = $1", hyp["situation_id"],
                    ) or ""
                )
            except Exception:  # pragma: no cover - best-effort
                sit_name = ""
        if not sit_name:
            skip_no_situation += 1
            continue
        entity_names = await _resolve_topic_entities(conn, name=sit_name)
        if not entity_names:
            skip_no_entities += 1
            continue

        try:
            fact_rows = await conn.fetch(
                """
                SELECT subject, predicate, value
                  FROM facts
                 WHERE superseded_by IS NULL AND valid_until IS NULL
                   AND produced_at > $1
                   AND lower(subject) = ANY($2::text[])
                 ORDER BY produced_at DESC
                 LIMIT $3
                """,
                produced_at, entity_names, MAX_EVIDENCE_PER_TOPIC,
            )
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("competing_hypotheses.resolve_facts_failed err=%s", exc)
            continue
        if len(fact_rows) < RESOLUTION_MIN_SUBSEQUENT_FACTS:
            skip_no_subsequent_facts += 1
            continue  # not enough subsequent evidence yet — stay unresolved

        # Net directional reading of the subsequent facts: score each against a
        # canonical "escalate" probe; the SIGN of the aggregate is the world's
        # subsequent direction.
        net = 0
        for fr in fact_rows:
            txt = f"{fr['subject']} {fr['predicate']} {fr['value']}"
            net += _score_consistency(txt, 0, "it will escalate")
        direction = _thesis_direction(thesis)
        if direction > 0:        # escalation thesis
            outcome = 1 if net > 0 else 0
        elif direction < 0:      # de-escalation thesis
            outcome = 1 if net < 0 else 0
        elif _thesis_is_status_quo(thesis):   # gradeable status-quo claim
            outcome = 1 if abs(net) <= 1 else 0
        else:
            # DQ-H2b — UNDIRECTED thesis: it makes no escalation/de-escalation/
            # status-quo claim we can grade against subsequent facts. The old
            # `else` graded it TRUE whenever facts were quiet (|net|<=1), which
            # is most of the time — auto-minting un-earned positives that
            # inflated the exogenous true-rate (directional rate was 0.097).
            # ABSTAIN: leave it unresolved so it never poisons the exogenous
            # Brier; a later LLM-entailment judge could grade it, but a cheap
            # keyword classifier must not pretend to.
            skip_undirected += 1
            continue

        try:
            await conn.execute(
                # D16 — stamped with the WEAK/LEXICAL label
                # (`_RESOLVED_BY_SUBSEQUENT_FACTS`) so calibration_tracking
                # DEMOTES it out of the headline exogenous Brier.
                # DQ P6 — ALSO transition ``status`` to the TERMINAL state that
                # matches the outcome (1 -> confirmed, 0 -> refuted) in the SAME
                # write. The exogenous resolver is the ONLY producer allowed to
                # reach a terminal status (hypothesis_lifecycle caps drift at the
                # working states supported/weakened), so a resolved row leaves the
                # active/working test pool and never double-counts in both the
                # active AND resolved pools (the 87-row inconsistency this fixes).
                # The terminal status is computed in Python and passed as its own
                # ($5) param — reusing $2 in a CASE made Postgres deduce it as both
                # smallint (the column) and integer (the literal compare).
                """
                UPDATE hypotheses
                   SET resolved_outcome = $2,
                       resolved_at = $3,
                       resolved_by = $4,
                       status = $5,
                       updated_at = $3
                 WHERE id = $1 AND resolved_outcome IS NULL
                """,
                hyp["id"], int(outcome), now, _RESOLVED_BY_SUBSEQUENT_FACTS,
                "confirmed" if int(outcome) == 1 else "refuted",
            )
            resolved += 1
            resolved_true += int(outcome)
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning("competing_hypotheses.resolve_write_failed err=%s", exc)

    # A-T3 sweep summary: where did the candidate set drop on its way to resolved?
    # DQ-H2b — report the DIRECTIONAL true-rate (resolved_true / resolved over the
    # gradeable, NON-abstained set) + the undirected-abstain count, so the
    # exogenous resolutions feeding calibration are honestly attributable.
    true_rate = (resolved_true / resolved) if resolved else None
    logger.info(
        "competing_hypotheses.resolve_subsequent_facts "
        "candidates=%s resolved=%s resolved_true=%s true_rate=%s "
        "skip_undirected=%s skip_no_situation=%s skip_no_entities=%s "
        "skip_no_subsequent_facts=%s min_age_days=%s",
        len(candidates), resolved, resolved_true,
        f"{true_rate:.3f}" if true_rate is not None else "n/a",
        skip_undirected, skip_no_situation, skip_no_entities,
        skip_no_subsequent_facts, RESOLUTION_MIN_AGE_DAYS,
    )

    return resolved, len(candidates)


async def _resolve_hypotheses_by_status_transition(
    conn: Any, *, now: datetime
) -> int:
    """SELF-CONSISTENCY resolution (FIX P1-2): stamp ``resolved_outcome`` for
    hypotheses that reached a TERMINAL status but were never exogenously
    resolved.

    A hypothesis whose ``status`` is ``confirmed`` (the lead crossed +CONFIRM_K)
    or ``refuted`` (a hypothesis crossed -REFUTE_K) but whose
    ``resolved_outcome IS NULL`` gets stamped: confirmed -> outcome 1, refuted ->
    outcome 0, ``resolved_at = now``, ``resolved_by = 'status_transition'``.

    HONESTY (read the module-level note on ``ENABLE_STATUS_TRANSITION_RESOLUTION``
    first): the same ``evidence_balance`` that drove the status transition ALSO
    derives the claimed confidence the Brier scores, so this resolution is a
    SELF-CONSISTENCY signal, NOT exogenous ground truth. It is labelled
    distinctly (``status_transition``) so calibration_tracking segregates it and
    flags the Brier as self-consistency. It NEVER overwrites a row already
    resolved exogenously (the ``resolved_outcome IS NULL`` filter), and the
    exogenous ``subsequent_facts`` resolver runs FIRST in ``run_method`` so any
    hypothesis that CAN be graded against subsequent facts is graded exogenously
    rather than self-consistently. Returns the count resolved.
    """
    try:
        result = await conn.execute(
            """
            UPDATE hypotheses
               SET resolved_outcome = CASE status
                                         WHEN 'confirmed' THEN 1
                                         WHEN 'refuted'   THEN 0
                                      END,
                   resolved_at = $1,
                   resolved_by = $2,
                   updated_at = $1
             WHERE resolved_outcome IS NULL
               AND status IN ('confirmed', 'refuted')
            """,
            now, _RESOLVED_BY_STATUS_TRANSITION,
        )
    except Exception as exc:
        logger.warning("competing_hypotheses.status_resolve_failed err=%s", exc)
        return 0
    try:
        return int(result.split()[-1]) if result else 0
    except (ValueError, IndexError):  # pragma: no cover - defensive
        return 0


# ---------------------------------------------------------------------------
# Public entry — run_method
# ---------------------------------------------------------------------------


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: ACHDeps | LLMHandlerLike | None,
) -> AnalystMethodResult:
    """Execute one ``competing_hypotheses`` (ACH) sweep.

    ``deps`` accepts an :class:`ACHDeps` (production) or a bare
    :class:`LLMHandlerLike` / ``None`` (the back-compat / no-pool test path).
    Returns an :class:`AnalystMethodResult` whose ``finding`` is the per-run
    summary; the HYPOTHESIS rows are side-written via ``write_hypothesis``.
    """
    if not isinstance(deps, ACHDeps):
        # bare LLM handle or None → wrap; no pool ⇒ topics must come from inputs.
        deps = ACHDeps(llm=deps if deps is not None else None)

    analyst_id = str(options.get("analyst_id") or KIND_NAME)
    analyst_version = str(options.get("analyst_version") or "")
    target_id = options.get("target_id")
    run_id = options.get("run_id")
    if isinstance(run_id, str):
        try:
            run_id = UUID(run_id)
        except ValueError:
            run_id = None
    if not isinstance(run_id, UUID):
        run_id = uuid4()
    system_prompt = deps.system_prompt or _SYSTEM_PROMPT

    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    pool = deps.pg_pool

    topics_analysed = 0
    hypotheses_written = 0
    confirmed = 0
    refuted = 0
    degraded = 0
    resolved_exogenous = 0
    resolved_self_consistency = 0

    # 1) Resolve the focal topics. Prefer the live situations sweep; the no-pool
    #    test path takes topics straight from inputs.
    topics: list[dict[str, Any]] = []
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                topics = await _read_focal_topics(
                    conn, limit=deps.max_topics, lookback_days=14,
                )
        except Exception as exc:
            logger.warning("competing_hypotheses.read_topics_failed err=%s", exc)
            topics = []
    if not topics:
        for row in inputs[: deps.max_topics]:
            name = row.get("name") or row.get("title") or row.get("topic")
            if name:
                topics.append({
                    "id": row.get("id") or row.get("situation_id"),
                    "name": str(name),
                    "category": str(row.get("category") or ""),
                    "status": str(row.get("status") or "active"),
                    "intensity_score": float(row.get("intensity_score") or 0.0),
                    "derived_from": list(row.get("derived_from") or []),
                })

    from ..provenance import AnalystContext  # local import — avoid import cycle

    for topic in topics:
        topic_name = str(topic.get("name") or "")
        # 2) Evidence base (temporally-current). No-pool: synthetic empty base.
        evidence: list[dict[str, Any]] = []
        if pool is not None:
            try:
                async with pool.acquire() as conn:
                    evidence = await _read_evidence_for_topic(
                        conn, situation=topic, limit=MAX_EVIDENCE_PER_TOPIC,
                    )
            except Exception as exc:  # pragma: no cover - context best-effort
                logger.warning(
                    "competing_hypotheses.evidence_failed topic=%s err=%s",
                    topic_name, exc,
                )
                evidence = list(topic.get("evidence") or [])
        else:
            evidence = list(topic.get("evidence") or [])

        # 3) Generate the competing hypotheses (LLM enrichment + fallback).
        hyps, usage, topic_degraded = await _generate_hypotheses(
            deps,
            topic_name=topic_name,
            category=str(topic.get("category") or ""),
            evidence=evidence,
            system_prompt=system_prompt,
        )
        for k in total_usage:
            total_usage[k] += int(usage.get(k, 0) or 0)
        if topic_degraded:
            degraded += 1
        topics_analysed += 1

        # 4) Build the ACH matrix + diagnosticity + integer balance + transitions.
        #    Score the consistency cells with the LLM (budget-gated, batched);
        #    any cell it omits / the whole matrix on failure falls back to the
        #    deterministic lexical scorer inside build_ach_matrix.
        cell_scores, matrix_usage = await _score_consistency_matrix_llm(
            deps, hypotheses=hyps, evidence=evidence,
        )
        for k in total_usage:
            total_usage[k] += int(matrix_usage.get(k, 0) or 0)
        ach = build_ach_matrix(hyps, evidence, cell_scores=cell_scores)

        # 5) Side-write one HYPOTHESIS row per competing hypothesis (the LIVE
        #    write_hypothesis path). The no-pool path counts the matrix but has
        #    nothing to persist.
        sit_id = topic.get("id")
        try:
            sit_uuid = sit_id if isinstance(sit_id, UUID) else (UUID(str(sit_id)) if sit_id else None)
        except (ValueError, TypeError):
            sit_uuid = None
        derived_uuids = [
            u for u in (
                m.get("evidence_id") for m in ach["matrix"]
            ) if u
        ]
        derived_uuids = [UUID(u) for u in derived_uuids if _is_uuid(u)]

        if pool is None:
            # no persistence path: still count what we'd write so tests over the
            # pure matrix can assert shape via build_ach_matrix directly.
            continue

        actx = AnalystContext(
            analyst_id=analyst_id,
            analyst_version=analyst_version,
            run_id=run_id,
            target_id=str(target_id) if target_id else None,
            target_version=options.get("target_version"),
        )
        for h_idx, h in enumerate(ach["hypotheses"]):
            status = _status_for(hypothesis_index=h_idx, ach=ach)
            # supporting/refuting signals: the DIAGNOSTIC evidence ids whose cell
            # for THIS hypothesis is consistent (support) / inconsistent (refute).
            supporting: list[UUID] = []
            refuting: list[UUID] = []
            for m in ach["matrix"]:
                if m["diagnosticity"] <= 0.0:
                    continue
                cell = m["cells"][h_idx]
                eid = m.get("evidence_id")
                if not _is_uuid(eid):
                    continue
                if cell["consistency"] > 0:
                    supporting.append(UUID(eid))
                elif cell["consistency"] < 0:
                    refuting.append(UUID(eid))
            payload = HypothesisPayload(
                thesis=str(h.get("thesis") or "")[:4096] or "(undetermined)",
                counter_thesis=str(h.get("counter_thesis") or "")[:4096],
                situation_id=sit_uuid,
                supporting_signals=supporting,
                refuting_signals=refuting,
                evidence_balance=int(ach["balance"][h_idx]),
                status=status,
                diagnostic_evidence=[{
                    "ach": True,
                    "hypothesis_index": h_idx,
                    "is_lead": h_idx == ach.get("lead_index"),
                    "balance": int(ach["balance"][h_idx]),
                    "matrix": ach["matrix"],
                    "evidence": ach["evidence"],
                    "competing_theses": [hh["thesis"] for hh in ach["hypotheses"]],
                    # how the consistency matrix was scored: "llm" when the
                    # per-cell LLM scorer ran, "lexical" when it fell back to the
                    # deterministic keyword scorer (budget-exhausted / no LLM).
                    "matrix_scorer": "llm" if cell_scores else "lexical",
                    "at": _now().isoformat(),
                }],
            )
            try:
                async with pool.acquire() as conn:
                    out, dlq = await write_hypothesis(
                        conn,
                        analyst_ctx=actx,
                        payload=payload,
                        derived_from=derived_uuids,
                    )
                if out is not None:
                    hypotheses_written += 1
                    confirmed += status == "confirmed"
                    refuted += status == "refuted"
                elif dlq is not None:
                    degraded += 1
                    logger.warning(
                        "competing_hypotheses.write_dlq topic=%s err=%s",
                        topic_name, getattr(dlq, "error", "?"),
                    )
            except Exception as exc:
                logger.warning(
                    "competing_hypotheses.write_failed topic=%s err=%s",
                    topic_name, exc,
                )
                degraded += 1

    # 6) EXOGENOUS resolution sweep — grade prior unresolved hypotheses against
    #    facts produced AFTER them (closes the circular Brier). This runs FIRST
    #    so any hypothesis that CAN be graded exogenously is, before the
    #    self-consistency fill below ever sees it. Best-effort: a failure here
    #    never fails the run (the producer half already landed).
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                resolved_exogenous, _ = await _resolve_hypotheses_against_subsequent_facts(
                    conn, now=_now(),
                )
        except Exception as exc:
            logger.warning("competing_hypotheses.resolution_sweep_failed err=%s", exc)

    # 6b) SELF-CONSISTENCY fill (FIX P1-2) — stamp resolved_outcome for terminal
    #     (confirmed/refuted) hypotheses still unresolved after the exogenous
    #     sweep, distinctly labelled `status_transition`. This is NOT exogenous
    #     ground truth (see the resolver's docstring); it only feeds the dormant
    #     calibration loop a numeric Brier where 0 exogenous outcomes exist, and
    #     calibration_tracking flags that Brier as self-consistency. Opt-out via
    #     options['enable_status_transition_resolution']=False.
    if pool is not None and bool(
        options.get(
            "enable_status_transition_resolution",
            ENABLE_STATUS_TRANSITION_RESOLUTION,
        )
    ):
        try:
            async with pool.acquire() as conn:
                resolved_self_consistency = (
                    await _resolve_hypotheses_by_status_transition(conn, now=_now())
                )
        except Exception as exc:
            logger.warning(
                "competing_hypotheses.status_resolution_sweep_failed err=%s", exc,
            )

    finding = _build_summary(
        topics=topics_analysed,
        hypotheses=hypotheses_written,
        confirmed=confirmed,
        refuted=refuted,
        degraded=degraded,
        resolved_exogenous=resolved_exogenous,
        resolved_self_consistency=resolved_self_consistency,
        target_id=str(target_id) if target_id else None,
    )
    return AnalystMethodResult(finding=finding, usage=total_usage)


def _is_uuid(raw: Any) -> bool:
    if isinstance(raw, UUID):
        return True
    if not isinstance(raw, str) or not raw:
        return False
    try:
        UUID(raw)
        return True
    except ValueError:
        return False


def _build_summary(
    *,
    topics: int,
    hypotheses: int,
    confirmed: int,
    refuted: int,
    degraded: int,
    resolved_exogenous: int = 0,
    resolved_self_consistency: int = 0,
    target_id: str | None,
) -> FindingPayload:
    title = (
        f"ACH: {hypotheses} competing hypotheses over {topics} topics "
        f"({confirmed} confirmed, {refuted} refuted, "
        f"{resolved_exogenous} exogenously resolved, "
        f"{resolved_self_consistency} self-consistency resolved)"
    )
    if target_id:
        title = f"{title} for {target_id}"
    tags = ["meta", "ach", "competing_hypotheses"]
    if degraded:
        tags.append("degraded")
    return FindingPayload(
        title=title[:2048],
        body=(
            f"topics={topics} hypotheses_written={hypotheses} "
            f"confirmed={confirmed} refuted={refuted} degraded={degraded} "
            f"resolved_exogenous={resolved_exogenous} "
            f"resolved_self_consistency={resolved_self_consistency}"
        )[:65536],
        confidence=1.0,
        tags=tags,
        data={
            "meta": True,
            "sub_handler": "competing_hypotheses",
            "topics": topics,
            "hypotheses_written": hypotheses,
            "confirmed": confirmed,
            "refuted": refuted,
            "degraded": degraded,
            "resolved_exogenous": resolved_exogenous,
            # SELF-CONSISTENCY (status_transition), NOT exogenous ground truth —
            # reported separately so consumers never conflate the two.
            "resolved_self_consistency": resolved_self_consistency,
        },
    )


__all__ = [
    "ACHDeps",
    "CONFIRM_K",
    "KIND_ALIASES",
    "KIND_NAME",
    "MIN_HYPOTHESES",
    "OUTPUT_KIND",
    "PROMPT_MODULE_PATH",
    "REFUTE_K",
    "build_ach_matrix",
    "build_prompt_module",
    "run_method",
]
