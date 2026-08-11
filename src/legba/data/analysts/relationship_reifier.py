# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``relationship_reifier`` — the PIECE A reified-typed-Nexus producer.

A META analyst kind (sibling of ``meta_findings_synthesizer`` /
``hypothesis_lifecycle``) that turns flat co-mentioned entity pairs into
FIRST-CLASS, signed, typed, temporally-bound ``nexus`` rows.

What it does, once per cadence tick (one global sweep — META analyst):

  1. **READ** candidate co-mentioned entity pairs from ``proposed_edges`` (the
     ``entity_resolution`` producer already lands ``co_occurs`` edges there),
     enriched with any recent ``facts`` bearing on the pair so the LLM has
     context. The AGE graph is consulted best-effort to skip pairs that are
     already reified.
  2. **TYPE** each candidate via the LLM provider plane (the D2 8B path — reuse
     the analyst LLM handle; NEVER litellm). The model assigns a typed
     ``rel_type`` (a canonical predicate), a canonical **polarity sign**
     (+1 supportive / -1 antagonistic / 0 neutral, the structural-balance
     convention), an ``intent``, a ``channel`` (direct/proxy/covert/...), and
     — when the relationship runs through a cut-out — an ``intermediary``.
  3. **WRITE** a ``nexus`` row per typed pair via the live ``write_nexus`` path
     (``valid_from`` = the pair's event time; supersession on a polarity/label
     CHANGE for the same typed triple). This is the same side-write discipline
     ``situation_clustering``/``hypothesis_lifecycle`` use: the nexus rows are
     the real output; the per-run ``FindingPayload`` summary
     (``candidates``/``typed``/``written``/``superseded``/``degraded``) is the
     cadence receipt.

Discipline:

  * **degrade-not-drop** — any per-candidate LLM/parse failure logs, flips the
    run's ``degraded`` counter, and skips THAT candidate; the sweep continues
    and still writes the candidates that did type. Mirrors ``fact_extractor``.
  * **budget plane** — the run checks ``deps.budget.check_envelope()`` before
    each LLM call and stops issuing new calls once the envelope is exhausted
    (the descriptor also caps ``budget_tokens_per_day``); per-run candidate
    count is hard-capped (``MAX_CANDIDATES_PER_RUN``). Token ``usage`` rolls up
    into the returned summary for the runtime's budget recorder.

The polarity sign is the load-bearing artifact: it is what lets the dormant
``structural_balance`` (signed-triad balance) + ``graph_mining`` (proxy-chain
sign products) consumers light up over a SIGNED graph instead of the untyped
``CoOccursWith`` edges they see today (PIECE A light-up).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable
from uuid import UUID

from ..provenance.models import FindingPayload, NexusPayload
from ..provenance.writes import write_nexus
from ...runtime.analyst_method import AnalystMethodResult, LLMHandlerLike

# The authoritative canonical edge-label → polarity table lives with the
# structural-balance handler (the consumer). The reifier maps the LLM's typed
# label through the SAME table so producer + consumer agree on the sign — there
# is one canonical POLARITY map in the tree, not two. ``polarity_from`` is the
# single deterministic intent/rel_type → sign function both sides use (D14) so a
# nexus's polarity can never contradict its declared intent.
from .deterministic_handlers.structural_balance import POLARITY, polarity_from

# SHARED CANON SPINE (W1 / remediation #1). Route nexus endpoints through the
# ONE canon every producer uses BEFORE building the nexus: collapse demonyms to
# their country ("Iranian" → "Iran") so "Israel leader of Israeli" / "Iran
# supplies weapons to Iranian" can never be reified, and DROP true junk
# endpoints. Prefer the new shared module path (the old deterministic_handlers
# path is now a re-export shim).
from .._entity_canon import canonicalize_entity, is_junk_entity, same_referent
# E1 — keeper election at write. Shared ``data``-layer helper (takes a conn; the
# canon must not) so a fragment/alias endpoint rewrites to its elected
# entity_profiles keeper BEFORE write_nexus. Degrade-not-break: never raises.
from .._entity_resolve import resolve_keeper
# K-G2 — the candidate WINDOW (pending-only + merge-aware bidirectional dedup)
# lives in its own module. ``MIN_EDGE_CONFIDENCE`` is a selection knob and is
# defined there; re-exported here because it was this module's name first.
from .edge_qualification import MIN_INDEPENDENT_SOURCES, RECOMMENDED_BAR
from .reifier_alias_pairs import record_alias_pair
from .reifier_selection import (
    MIN_EDGE_CONFIDENCE,
    SelectionCounters,
    select_candidates,
)

logger = logging.getLogger(__name__)

KIND_NAME: str = "relationship_reifier"
HANDLER_VERSION: str = "0.1.0"
# K-3: this named `legba.prompts.relationship_reifier.v1` — a package that has
# never existed. Unlike the DSPy-backed kinds, this one carries its prompt in
# `_SYSTEM_PROMPT` below and exports no `build_prompt_module`, so the dotted
# path was aspirational and no reader ever noticed. Points at the real constant
# now; the descriptor's `method.prompt_module` mirrors it.
PROMPT_MODULE_PATH: str = "legba.data.analysts.relationship_reifier:_SYSTEM_PROMPT"

# OUTPUT_KIND is TRACE_ONLY: this META analyst's REAL product is the `nexus`
# rows it side-writes via write_nexus on the run's own connection. The per-run
# summary FindingPayload it returns is purely a run-receipt — and every run is
# already fully audited in `analyst_traces` (the summary survives in
# `analyst_traces.output_payload`). Marking it TRACE_ONLY stops the redundant
# FINDING row in `analyst_outputs` ("Findings as a real output type" cleanup)
# while keeping the trace + the write_nexus side-writes intact. `run_method`
# still returns AnalystMethodResult(finding=<summary>) so the trace captures it.
from ..provenance.kinds import TRACE_ONLY as _TRACE_ONLY  # noqa: E402
from ..provenance.kinds import OutputKind as _OutputKind  # noqa: E402,F401

OUTPUT_KIND: object = _TRACE_ONLY


# ---------------------------------------------------------------------------
# Knobs
# ---------------------------------------------------------------------------

DEFAULT_MAX_TOKENS: int = 384
"""Completion budget per typing call. The output is one tiny typed JSON object,
not prose — keep it small (8B path, cost-sensitive)."""

DEFAULT_TEMPERATURE: float = 0.1
"""Typing wants determinism."""

MAX_CANDIDATES_PER_RUN: int = 40
"""Hard cap on co-mentioned pairs typed per cadence tick. Bounds the per-run
LLM spend regardless of how many pending edges exist."""

DEFAULT_BATCH_SIZE: int = 12
"""Candidates per typing call. MEASURED (``docs/TYPING_BAKEOFF_2026-08-03.md``
§7.2-7.3), not assumed: at N=12 the core 120B returned 17/17 clean calls over
200 candidates with zero truncation, and prompt tokens per candidate fall from
1,462 (one call per candidate) to 297 — a 4.9× reduction, because the system
preamble and the whole allowed-``rel_type`` vocabulary are stated ONCE per call
instead of once per pair. N=24 and N=40 save more but rest on a single
observation each, and N=40 showed a possible judgement shift (accepts 47.5% →
35%) that must be ruled out before adoption."""

#: THERE IS NO ESCALATION LADDER, AND THAT IS THE BAKE-OFF'S FINDING.
#:
#: A two-tier "type it cheap, escalate the hard ones" router needs a difficulty
#: signal. K-G2 looked for one — qualification score, evidence length,
#: independent-source count — and NONE predicts model disagreement (§6.2):
#: three-way unanimity is flat at 41-48% across every stratum tested.
#:
#: The reason is the ceiling. ``core120b`` and the OpenRouter ``gpt-oss-120b``
#: are the SAME WEIGHTS on identical frozen prompts at temperature 0.1, and they
#: agree on edge-vs-reject only 79.6% of the time — **Cohen's κ = 0.589**. The
#: model does not agree with ITSELF on one candidate in five, so ~40% of what
#: looks like model disagreement is irreducible sampling noise on a task this
#: underdetermined. Absent a usable difficulty signal, a ladder would route by
#: coin-flip and pay a second model for the privilege; and any model swap
#: re-rolls ~20% of the graph's edges whichever model "wins".
#:
#: So: ONE typer, on the $0 self-hosted core plane, until the operator's
#: hand-check labels (docs/data/kg2_bakeoff/handcheck_worksheet.csv) provide the
#: ground truth to build a real router on. Do not add a fallback tier here
#: without that.
SINGLE_TYPER_RATIONALE: str = "kg2_kappa_0.589_self_agreement"

MAX_FACTS_CONTEXT: int = 6
"""Recent facts about either endpoint rendered into the typing prompt."""

MAX_INTERMEDIARY_CANDIDATES: int = 5
"""Cap on cut-out candidates offered to the typer per pair (#99). The model
SELECTS an intermediary from this OFFERED set (or null) — it never free-texts a
famous-but-absent proxy. Kept small to bound prompt tokens."""

MIN_INTERMEDIARY_PAIR_CONFIDENCE: float = 0.55
"""Only the more-corroborated (A,B) pairs get the (more expensive) 3-entity
candidate path. A bare single co-mention is too thin to chase a cut-out."""

# Canonical relationship-type set the LLM may pick from. This is the POLARITY
# table's key set — the model is constrained to labels we can sign. Anything
# off-list maps to polarity 0 (neutral) at coercion time.
ALLOWED_REL_TYPES: tuple[str, ...] = tuple(POLARITY.keys())

_VALID_CHANNELS: frozenset[str] = frozenset(
    {"direct", "proxy", "covert", "institutional"}
)
_VALID_INTENTS: frozenset[str] = frozenset(
    {"supportive", "hostile", "dual-use", "neutral"}
)


# ---------------------------------------------------------------------------
# Deps surface — LLM port + pg_pool (the reifier reads candidates + recent
# facts and side-writes nexus rows on its own connection, like the
# deterministic META handlers; it is NOT a pure inputs->finding kind).
# ---------------------------------------------------------------------------


@runtime_checkable
class _BudgetLike(Protocol):
    async def check_envelope(self) -> str: ...


@dataclass
class ReifierDeps:
    """The dep bundle ``run_method`` needs.

    Built by ``analyst_deps_builder._build_relationship_reifier`` from the
    resolved primary LLM + the run's ``StandardDeps`` (pg_pool + budget). Tests
    construct it directly with a stub LLM + a real test pg_pool.
    """

    llm: LLMHandlerLike
    pg_pool: Any = None
    budget: _BudgetLike | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    max_candidates: int = MAX_CANDIDATES_PER_RUN
    #: Candidates per typing call — see :data:`DEFAULT_BATCH_SIZE`. 1 restores
    #: the pre-K-G2 one-call-per-candidate shape (still a correct path; it is
    #: what the straggler retry uses), at 4.9× the prompt tokens.
    batch_size: int = DEFAULT_BATCH_SIZE
    #: The qualification bar a candidate must clear to earn a typing call, and
    #: the hard independent-source floor beneath it. Both from K-G2 §7.4;
    #: :mod:`.edge_qualification` owns the definitions.
    qualification_bar: float = RECOMMENDED_BAR
    min_independent_sources: int = MIN_INDEPENDENT_SOURCES
    system_prompt: str | None = None


# ---------------------------------------------------------------------------
# Typing prompt
# ---------------------------------------------------------------------------

from ._tradecraft import with_preamble  # noqa: E402

_SYSTEM_PROMPT = with_preamble(
    """TASK — type the relationship between two co-mentioned entities. Decide whether a meaningful, directed relationship holds and, if so, classify it.
Return ONE JSON object, nothing else:
{
  "same_entity": true|false,        // true => A and B are two NAMES for ONE entity
  "related": true|false,            // false => no real relationship; skip
  "subject": "<acting entity>",     // who initiates/conducts
  "object": "<affected entity>",    // who is targeted/affected
  "intermediary": "<proxy>"|null,   // a cut-out the relationship runs through, else null
  "rel_type": "<one of the allowed types>",
  "polarity": -1|0|1,               // -1 antagonistic, +1 supportive, 0 neutral/dual-use
  "intent": "supportive"|"hostile"|"dual-use"|"neutral",
  "channel": "direct"|"proxy"|"covert"|"institutional",
  "confidence": 0.0-1.0
}
Rules: pick rel_type ONLY from the allowed list. If merely co-mentioned with no real relationship, set related=false.
SAME-ENTITY CHECK FIRST: if A and B are two names for the SAME thing (an acronym and its expansion — IRGC / Revolutionary Guards Corps — an abbreviation, a former name, a translation, a nickname), set same_entity=true and related=false. An entity is not related to itself. A part-whole or membership relation is NOT this: a subsidiary, a subcommittee, a province or a member state is a DIFFERENT entity from its parent.
INTERMEDIARY rule: set "intermediary" to null UNLESS a "Candidate intermediaries" list is offered AND one of those listed entities genuinely acts as the cut-out the A->B relationship runs through. You MUST copy the intermediary VERBATIM from the offered list — never name a proxy that is not on the list, however plausible. If no offered candidate fits, intermediary=null and channel is direct/institutional as appropriate.
Worked examples:
  - Hostile supply via a proxy: A arms a militia that attacks B -> subject=A, object=B, intermediary=the militia, rel_type=SuppliesWeaponsTo, polarity=-1, channel=proxy, intent=hostile.
  - Institutional membership: country X joins alliance Y -> subject=X, object=Y, intermediary=null, rel_type=MemberOf, polarity=+1, channel=institutional, intent=supportive.
  - Dual-use presence: company C operates a facility in country D with no stated alignment -> subject=C, object=D, intermediary=null, rel_type=OperatesIn, polarity=0, channel=direct, intent=dual-use."""
)


def _build_user_prompt(
    *,
    source: str,
    target: str,
    evidence_text: str,
    facts: Sequence[Mapping[str, Any]],
    candidate_intermediaries: Sequence[str] = (),
) -> str:
    lines = [
        f"Entity A: {source}",
        f"Entity B: {target}",
        f"Allowed rel_type values: {', '.join(ALLOWED_REL_TYPES)}",
        "",
        "Co-mention evidence:",
        (evidence_text or "(none)")[:1200],
    ]
    if facts:
        lines.append("")
        lines.append("Recent facts about these entities:")
        for f in facts[:MAX_FACTS_CONTEXT]:
            lines.append(
                f"  - {f.get('subject')} {f.get('predicate')} {f.get('value')}"
            )
    if candidate_intermediaries:
        lines.append("")
        lines.append(
            "Candidate intermediaries (third entities co-mentioned with BOTH "
            "A and B). SELECT one ONLY if it is the cut-out the A->B "
            "relationship runs through — copy it verbatim — else use null:"
        )
        for c in candidate_intermediaries:
            lines.append(f"  - {c}")
    lines.append("")
    lines.append("Classify the relationship as the JSON object specified.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM call + parse
# ---------------------------------------------------------------------------


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    """Best-effort: pull the first balanced ``{...}`` object out of an LLM
    response (handles ```json fences + leading prose). Mirrors the
    meta_findings_synthesizer parser. Returns None on failure (degrade)."""
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
# D14 — sports / event co-occurrence gate.
#
# The live review found co-occurrence of two entities inside a sports fixture /
# match report ("Spain hostile to Saudi Arabia", "Iran hostile to Group G")
# being reified as signed -1 "hostile" GEOPOLITICS. A World-Cup group draw is
# NOT a hostile relationship. When the evidence context is a sports/match frame,
# a HOSTILE typing is downgraded to a NEUTRAL co-occurrence (polarity 0,
# intent=neutral) — we keep the pair as a benign edge instead of poisoning the
# signed graph with fake antagonism.
# ---------------------------------------------------------------------------

import re as _re  # noqa: E402

# DQ Phase 5 r2 — the sports gate is SPLIT into two tiers so it can NEVER
# suppress genuine interstate hostility. The round-1 extension folded DUAL-USE
# conflict words (clash / derail / squad / coach) straight into the trigger, so
# real war / diplomacy reporting was misread as a football fixture (live
# proposed-edge evidence: "225 clashes on the front line", "UN Security Council
# members clash over ...", "long-running clash" over a trade dispute).
#
# Two tiers, both matched case-insensitively over the co-mention evidence text:
#   (1) UNAMBIGUOUS — vocabulary that marks a sports frame ON ITS OWN ("world
#       cup", "knockout", "winger", a scoreline "beat X 2-1", the "<team> face
#       <Team> with …" fixture framing). A bare "league"/"final" is NOT here
#       (they recur in geopolitics — "Arab League", "final round of talks"); the
#       NAMED leagues (premier/champions/la liga…) are.
#   (2) DUAL-USE — words that ALSO recur in conflict/diplomacy reporting
#       (clash/derail/squad/coach/kick/goal). A dual-use word counts as sports
#       ONLY when an explicit sports ANCHOR (world cup / match / league /
#       tournament / cup / fixture / group stage / final / penalty / goalkeeper
#       / stadium / score…) co-occurs in the SAME text.
#
# _is_sports_context is True iff (1) matches OR ((2) matches AND an anchor
# matches). So "clash" alone (Russia/Ukraine front line) is NOT sports; "clash"
# + "World Cup" IS — a hostile edge still reifies, a fixture is downgraded.

#: (1) UNAMBIGUOUS sports vocabulary — a hit here alone marks a sports frame.
_SPORTS_UNAMBIGUOUS_RE = _re.compile(
    r"(?:\b(?:"
    r"world\s+cup|"
    r"group\s+(?:stage|[a-h])\b|"
    r"qualifier|qualifiers|qualifying|"
    r"friendly\s+match|"
    r"kick[\s-]?off|"
    r"football|soccer|"
    r"la\s+liga|premier\s+league|bundesliga|serie\s+a\b|ligue\s+1|"
    r"champions\s+league|europa\s+league|"
    r"fifa|uefa|"
    r"semi[\s-]?finals?|quarter[\s-]?finals?|penalty\s+shootout|"
    r"olympic|olympics|"
    r"knockout|fullback|full[\s-]?back|winger|"
    r"midfielder|striker|goalkeeper|penalty\s+kick|"
    r"cricket|rugby|tennis|basketball|"
    r"match\s+(?:report|preview|day)|"
    r"fixtures?"
    r")\b)"
    # a football scoreline framed as a result ("beat Morocco 2-1", "won 3–0",
    # "lost 0-2 on penalties"). Guarded by a result verb (a team name may sit
    # between the verb and the score) so a bare numeric range never trips it; the
    # score halves are 1–2 digits so a 4-digit year cannot match.
    r"|\b(?:beat|beats|won|win|lost|draw|drew|thrash(?:ed|es)?|defeat(?:ed|s)?)"
    r"\b[\w\s.,'-]{0,24}?\b\d{1,2}\s*[-–:]\s*\d{1,2}\b"
    # the World-Cup fixture framing "<team> face <Team> with …" ("DR Congo face
    # England with nothing to lose"). Scoped case-SENSITIVE (?-i:) so the object
    # must be a capitalised proper noun (a team/country) — "face them with force"
    # is not a fixture.
    r"|(?-i:\b[Ff]aces?\s+[A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,2}\s+[Ww]ith\b)",
    _re.IGNORECASE,
)

#: (2) DUAL-USE tokens — sports OR conflict/diplomacy. Only count as sports when
#: an anchor (below) co-occurs. clash/derail/squad/coach/kick/goal (+ inflections).
_SPORTS_DUAL_USE_RE = _re.compile(
    r"\b(?:"
    r"clash(?:es|ed|ing)?|derail(?:ed|s|ing)?|squads?|"
    r"coach(?:es|ed|ing)?|kicks?|goals?"
    r")\b",
    _re.IGNORECASE,
)

#: Explicit sports ANCHORS that promote a co-occurring DUAL-USE word to a sports
#: frame. Curated to strongly-sports contexts; a bare dual-use word without one
#: of these reads as conflict/diplomacy, not sports. NOTE: bare "score"/"scores"
#: is deliberately EXCLUDED (only "scoreline") — "scores of civilians killed in
#: the clash" is a casualty count, not a football score, and must stay hostile.
_SPORTS_ANCHOR_RE = _re.compile(
    r"\b(?:"
    r"world\s+cup|match(?:es|day)?|leagues?|tournaments?|cups?|fixtures?|"
    r"group\s+stage|finals?|penalt(?:y|ies)|goalkeeper|stadiums?|"
    r"scoreline|kick[\s-]?off|"
    r"football|soccer|fifa|uefa|olympics?"
    r")\b",
    _re.IGNORECASE,
)


#: DQ Phase 5 (nexuses / endpoint hygiene) — citation-marker residue NER dragged
#: into an entity surface: '/*[1]*/', '[1]', '[1' (unclosed), '【1】', '［1］'.
#: A real entity name never contains a citation bracket, so stripping it before
#: the endpoint is canonicalized/written prevents 'Masoud Pezeshkian /*[1]*/' and
#: 'Donald Trump[1' from ever landing as nexus endpoints again.
_CITATION_COMMENT_RE = _re.compile(r"/\*.*?\*/")
_CITATION_BRACKET_RE = _re.compile(r"[【［〔〖\[]\s*\d*\s*[】］〕〗\]]?")
_CITATION_STRAY_CLOSE_RE = _re.compile(r"[】］〕〗\]]")


def _strip_citation_residue(surface: str) -> str:
    """Remove '[N]' / '【N】' / '/*[N]*/' citation residue from an entity surface
    before it is canonicalized and written. Idempotent on clean surfaces (a name
    with no bracket is returned unchanged, whitespace-collapsed)."""
    s = str(surface or "")
    s = _CITATION_COMMENT_RE.sub(" ", s)
    s = _CITATION_BRACKET_RE.sub(" ", s)
    s = _CITATION_STRAY_CLOSE_RE.sub(" ", s)
    return _re.sub(r"\s+", " ", s).strip()


#: DQ Phase 5 (confidence semantics) — an exact-1.0 reified-nexus confidence is
#: the "no real score" sentinel (mirrors the facts relation-backend floor); a
#: reified relationship is never certain. Floored to this documented value.
_NEXUS_SENTINEL_FLOOR: float = 0.5


def _is_sports_context(evidence_text: str) -> bool:
    """True when the co-mention evidence reads as a sports / fixture frame (D14).

    Pure. A hostile typing over a sports co-mention is a false antagonism — the
    caller downgrades it to a neutral co-occurrence so the signed graph is not
    poisoned with "<country> hostile to <country>" from a match report.

    Two-tier (DQ P5 r2): an UNAMBIGUOUS sports hit alone qualifies; a DUAL-USE
    word (clash/derail/squad/coach/…) qualifies ONLY when an explicit sports
    ANCHOR co-occurs — so genuine interstate hostility ("225 clashes on the
    front line") is never mis-gated as a fixture."""
    text = str(evidence_text or "")
    if not text:
        return False
    if _SPORTS_UNAMBIGUOUS_RE.search(text):
        return True
    return bool(
        _SPORTS_DUAL_USE_RE.search(text) and _SPORTS_ANCHOR_RE.search(text)
    )


# ---------------------------------------------------------------------------
# FU4 (round 2) — conflict / casualty guard on the sports downgrade.
#
# The D14 sports gate now runs over the UNION of the co-mention excerpt + ALL
# backing source signals' title+summary (FU4 round 1). A proposed_edge
# accumulates EVERY co-mention signal into ``derived_from``, so a genuinely
# HOSTILE dyad (Gaza/Israel, Russia/Ukraine) that ever shared ONE signal
# carrying sports vocabulary ("World Cup") would have the whole union read as a
# sports frame → the real hostility downgraded to CoOccursWith / polarity 0.
#
# Guard: the sports downgrade fires ONLY when the union has a sports frame AND
# NO conflict/casualty vocabulary anywhere in the union. If ANY backing signal
# carries conflict vocab, the dyad is treated as real and the reifier's normal
# hostility typing proceeds (no downgrade). A PURE sports fixture (sports frame,
# no conflict vocab) is still downgraded — the FU4 round-1 gain is preserved.
#
# Curated to strongly-conflict, minimally sports-ambiguous terms (anchored on
# word boundaries). Err toward NOT downgrading: the far worse error is erasing a
# real interstate hostility, so a couple of dual-use conflict words (offensive /
# war / troops / sanctions) that also brush sports contexts are accepted here.
_CONFLICT_CASUALTY_RE = _re.compile(
    r"\b(?:"
    r"kill(?:ed|ings?)|casualt(?:y|ies)|wounded|"
    r"air[\s-]?strikes?|shell(?:ed|ing)|bombard(?:ed|ment|ing)|"
    r"bomb(?:ed|ing)|bombings?|missiles?|drones?|artillery|"
    r"war|warfare|wartime|war\s+crimes?|invasion|invad(?:e|ed|ing)|"
    r"offensive|siege|besieg(?:e|ed|ing)|ceasefire|cease[\s-]?fire|truce|"
    r"sanctions|troops|soldiers|militants?|insurgents?|"
    r"front[\s-]?line|genocide|massacres?|atrocit(?:y|ies)|combat|military"
    r")\b",
    _re.IGNORECASE,
)


def _has_conflict_context(text: str) -> bool:
    """True when the text carries CONFLICT / CASUALTY vocabulary (FU4 round 2).

    Pure. Used to BLOCK the sports downgrade: a real hostile dyad whose lineage
    union happens to include a stray sports signal must never be erased. The
    downgrade fires only when a sports frame is present AND this returns False."""
    return bool(_CONFLICT_CASUALTY_RE.search(str(text or "")))


def _canonical_polarity(rel_type: str, intent: Any) -> int:
    """Resolve the canonical sign DETERMINISTICALLY from (intent, rel_type) — D14.

    Delegates to the ONE shared :func:`polarity_from` (lives with the POLARITY
    table) so producer + consumer sign identically and the sign is a PURE
    function of the declared intent / rel_type. The LLM's free ``polarity``
    integer is NO LONGER consulted: it was the source of the polarity≠intent
    contradictions the review flagged. ``intent`` wins when known
    (supportive→+1, hostile/conflict→-1, neutral/dual-use→0); else the rel_type
    table; else 0."""
    return polarity_from(intent, rel_type)


def _coerce_typing(
    obj: Mapping[str, Any],
    *,
    fallback_subject: str,
    fallback_object: str,
    allowed_intermediaries: Sequence[str] = (),
    evidence_text: str = "",
) -> NexusPayload | None:
    """Turn the parsed LLM object into a validated :class:`NexusPayload`, or
    ``None`` when the model said there is no real relationship / the shape is
    unusable / an endpoint is junk or self-referential after canonicalization
    (degrade-not-drop: a None just skips this candidate).

    ``allowed_intermediaries`` is the OFFERED cut-out set (#99). A returned
    intermediary that is not in this set is dropped to null — the typer SELECTS
    or nulls, it may never free-text a famous-but-absent proxy. When no set is
    offered, any returned intermediary is also dropped (no candidate path ran).

    ``evidence_text`` is the co-mention context, consulted ONLY for the D14
    sports/event gate (a hostile typing over a sports fixture is downgraded to a
    neutral co-occurrence).

    D3: subject / object / intermediary are routed through the shared
    :func:`canonicalize_entity` BEFORE the nexus is built — demonyms collapse to
    their country ("Israeli" → "Israel"), so a self-loop like "Israel LeaderOf
    Israeli" or "Iran SuppliesWeaponsTo Iranian" canonicalizes to subject ==
    object and is DROPPED; true junk endpoints (:func:`is_junk_entity`) are also
    dropped.
    """
    if not obj.get("related", False):
        return None
    rel_type = str(obj.get("rel_type") or "").strip()
    if rel_type not in ALLOWED_REL_TYPES:
        # Off-list label — the consumers can't sign it; skip rather than write a
        # neutral nexus that adds no signal.
        return None
    # DQ Phase 5 — strip citation-marker residue ('[1]' / '/*[2]*/' / '【3】')
    # from the endpoint surfaces BEFORE the junk check + canonicalization, so a
    # contaminated name ('Masoud Pezeshkian /*[1]*/', 'Donald Trump[1') can never
    # land as a nexus endpoint (and its clean form supersedes correctly).
    raw_subject = _strip_citation_residue(
        str(obj.get("subject") or fallback_subject).strip()
    )
    raw_object = _strip_citation_residue(
        str(obj.get("object") or fallback_object).strip()
    )
    if not raw_subject or not raw_object:
        return None
    # D3 — DROP true junk endpoints, then CANONICALIZE (demonym → country, HTML
    # strip, alias merge) so the nexus is built over the canonical referents.
    if is_junk_entity(raw_subject) or is_junk_entity(raw_object):
        return None
    subject, _ = canonicalize_entity(raw_subject, "entity")
    object_, _ = canonicalize_entity(raw_object, "entity")
    # canonicalize_entity returns "" for a fully-stripped-away / junk-collapsed
    # name — drop. A demonym collapse can also make subject == object (the
    # "Israel leader of Israeli" / "Iran supplies weapons to Iranian" class):
    # that is a self-loop, not a relationship — DROP it. DQ M8: same_referent
    # also catches a plain singular/plural self-loop ("Houthi"/"Houthis") the
    # canon does not map to a single lemma.
    if not subject or not object_ or same_referent(subject, object_):
        return None
    intermediary = obj.get("intermediary")
    intermediary = (
        str(intermediary).strip() if intermediary not in (None, "", "null") else None
    )
    # SELECT-or-null enforcement: an intermediary survives ONLY if it is one of
    # the offered candidates (case-insensitive) and is distinct from both
    # endpoints. Anything else (a hallucinated proxy, or one returned when no
    # candidates were offered) is nulled — the relationship stays direct. The
    # offered-set comparison is on the RAW offered names (the typer copies them
    # verbatim); the surviving intermediary is then canonicalized like the
    # endpoints and re-checked for collision / junk.
    if intermediary is not None:
        _allowed = {c.strip().lower() for c in allowed_intermediaries if c.strip()}
        if intermediary.lower() not in _allowed:
            intermediary = None
        elif is_junk_entity(intermediary):
            intermediary = None
        else:
            canon_inter, _ = canonicalize_entity(intermediary, "entity")
            if (
                not canon_inter
                or canon_inter.lower() == subject.lower()
                or canon_inter.lower() == object_.lower()
            ):
                intermediary = None
            else:
                intermediary = canon_inter
    # D14 — intent first (it drives the deterministic polarity). Validate against
    # the closed intent set; an unknown intent is resolved from the rel_type
    # table sign below.
    intent = str(obj.get("intent") or "").strip().lower()
    if intent not in _VALID_INTENTS:
        intent = ""  # unknown → let polarity_from fall back to the rel_type table
    # D14 SPORTS GATE — a hostile typing over a sports/fixture co-mention is a
    # false antagonism (World-Cup group draw ≠ geopolitics). Downgrade it to a
    # neutral co-occurrence so the signed graph is not poisoned.
    #
    # FU4 (round 2) — the gate runs over the UNION of the excerpt + all backing
    # source signals (see :func:`_sports_gate_text`); a genuinely hostile dyad
    # that ever shared one signal carrying sports vocab must NOT be downgraded.
    # So the downgrade fires only when a sports frame is present AND NO conflict/
    # casualty vocabulary sits anywhere in the union — otherwise it is a real
    # dyad and the normal hostility typing proceeds.
    if (
        intent == "hostile"
        and _is_sports_context(evidence_text)
        and not _has_conflict_context(evidence_text)
    ):
        intent = "neutral"
        rel_type = "CoOccursWith" if "CoOccursWith" in ALLOWED_REL_TYPES else rel_type
        logger.info(
            "relationship_reifier.sports_gate downgraded hostile pair=%s/%s",
            subject, object_,
        )
    # D14 — polarity is now a PURE function of (intent, rel_type); the LLM's free
    # polarity integer is no longer consulted (it was the contradiction source).
    polarity = _canonical_polarity(rel_type, intent)
    # Backfill a still-empty intent FROM the resolved sign so the row carries a
    # coherent intent string (and intent ⇔ polarity stay consistent).
    if not intent:
        intent = "hostile" if polarity < 0 else ("supportive" if polarity > 0 else "neutral")
    channel = str(obj.get("channel") or "direct").strip().lower()
    if channel not in _VALID_CHANNELS:
        channel = "proxy" if intermediary else "direct"
    # A "proxy" channel is meaningless without a cut-out — if the intermediary
    # was nulled (hallucinated / not offered), the relationship is direct.
    if channel == "proxy" and not intermediary:
        channel = "direct"
    try:
        confidence = float(obj.get("confidence", 0.6))
    except (TypeError, ValueError):
        confidence = 0.6
    confidence = max(0.0, min(1.0, confidence))
    # DQ Phase 5 — an exact 1.0 is the "no real score" sentinel (mirrors the
    # facts relation-backend floor): a reified relationship is never certain.
    # Floor it so a syndicated co-mention can't manufacture a 1.0 nexus.
    if confidence >= 1.0:
        confidence = _NEXUS_SENTINEL_FLOOR
    try:
        return NexusPayload(
            subject=subject[:2048],
            intermediary=(intermediary[:2048] if intermediary else None),
            object=object_[:2048],
            rel_type=rel_type,
            label=f"{subject} {rel_type} {object_}"[:4096],
            polarity=polarity,
            intent=intent,
            channel=channel,
            confidence=confidence,
        )
    except Exception as exc:  # pragma: no cover - pydantic guard
        logger.warning("relationship_reifier.coerce_failed err=%s", exc)
        return None


# ---------------------------------------------------------------------------
# Substrate reads (own connection — the META/maintenance precedent)
# ---------------------------------------------------------------------------


async def _read_candidates(
    conn: Any, *, limit: int, keeper_cache: dict[str, str] | None = None
) -> list[dict[str, Any]]:
    """The per-run candidate window — see :mod:`.reifier_selection`.

    K-G2 replaced this function's body. It used to read the WHOLE
    ``proposed_edges`` table (no ``status`` filter) and dedup against the RAW
    endpoint surfaces, which the keeper rewrite at write time had already made
    unmatchable. Measured against the live substrate, the resulting top-40
    window contained **zero** pending rows — 24 ``orphaned``, 15 ``promoted``,
    1 ``rejected`` — so every typing call was structurally incapable of
    producing an edge (``docs/TYPING_BAKEOFF_2026-08-03.md`` §1).

    The selection now lives in its own module because it grew a second stage
    (keeper resolution + a bulk bidirectional dedup probe) that has nothing to
    do with typing. This wrapper stays so the existing call sites and tests keep
    their signature; it returns the window and drops the counters, which
    ``run_method`` reads directly from :func:`~.reifier_selection.select_candidates`.
    """
    rows, _counters = await select_candidates(
        conn, limit=limit, keeper_cache=keeper_cache
    )
    return rows


def _sports_gate_text(cand: Mapping[str, Any]) -> str:
    """The text the D14 sports gate runs over (FU4): the co-mention EXCERPT
    UNIONED with ALL backing source signals' title+summary. A sports fixture whose
    sports frame ('World Cup') sits in a DIFFERENT source signal than the excerpt
    is thus still gated. Falls back to the excerpt alone on the no-pool / test
    path (where ``source_signal_text`` is absent). This union feeds ONLY the gate
    — the LLM typing prompt keeps the terse excerpt (no token bloat)."""
    parts = [
        str(cand.get("evidence_text") or ""),
        str(cand.get("source_signal_text") or ""),
    ]
    return " ".join(p for p in parts if p.strip())


async def _intermediary_candidates_for(
    conn: Any, *, source: str, target: str, limit: int
) -> list[str]:
    """Third entities C co-mentioned with BOTH A and B (#99 proxy-chain path).

    A ``co_occurs`` edge in ``proposed_edges`` is undirected for this purpose, so
    C is any entity that shares a co_occurs edge with A AND a (distinct) co_occurs
    edge with B — a structurally-plausible cut-out for the A->B relationship. We
    return only NAMES (the typer SELECTS verbatim from this offered set, never
    free-texts), ordered by combined corroboration so the strongest cut-outs come
    first within the small cap. C is never A or B."""
    rows = await conn.fetch(
        """
        WITH neighbours AS (
            SELECT
                CASE WHEN lower(source_entity) = lower($1)
                     THEN target_entity ELSE source_entity END AS c,
                confidence,
                $1 AS anchor
              FROM proposed_edges
             WHERE relationship_type = 'co_occurs'
               AND (lower(source_entity) = lower($1)
                    OR lower(target_entity) = lower($1))
            UNION ALL
            SELECT
                CASE WHEN lower(source_entity) = lower($2)
                     THEN target_entity ELSE source_entity END AS c,
                confidence,
                $2 AS anchor
              FROM proposed_edges
             WHERE relationship_type = 'co_occurs'
               AND (lower(source_entity) = lower($2)
                    OR lower(target_entity) = lower($2))
        )
        SELECT c, sum(confidence) AS score
          FROM neighbours
         WHERE lower(c) <> lower($1)
           AND lower(c) <> lower($2)
         GROUP BY lower(c), c
        HAVING count(DISTINCT lower(anchor)) = 2
         ORDER BY score DESC
         LIMIT $3
        """,
        source,
        target,
        limit,
    )
    return [str(r["c"]) for r in rows]


async def _recent_facts_for(
    conn: Any, *, source: str, target: str
) -> list[dict[str, Any]]:
    """Open facts whose subject is either endpoint — context for the typer."""
    rows = await conn.fetch(
        """
        SELECT subject, predicate, value
          FROM facts
         WHERE valid_until IS NULL AND superseded_by IS NULL
           AND (lower(subject) = lower($1) OR lower(subject) = lower($2))
         ORDER BY confidence DESC, produced_at DESC
         LIMIT $3
        """,
        source,
        target,
        MAX_FACTS_CONTEXT,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# LLM port
# ---------------------------------------------------------------------------


async def _type_via_llm(
    llm: LLMHandlerLike,
    *,
    user_prompt: str,
    system_prompt: str,
    max_tokens: int,
    temperature: float,
) -> tuple[str, dict[str, int]]:
    """One chat_complete typing call. Mirrors the sibling kinds' shape."""
    messages = [{"role": "user", "content": user_prompt}]
    response = await llm.chat_complete(
        messages,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt,
    )
    content = getattr(response, "content", "") or ""
    usage_raw = getattr(response, "usage", None)
    usage = {
        "prompt_tokens": getattr(usage_raw, "prompt_tokens", 0) if usage_raw else 0,
        "completion_tokens": (
            getattr(usage_raw, "completion_tokens", 0) if usage_raw else 0
        ),
        "reasoning_tokens": (
            getattr(usage_raw, "reasoning_tokens", 0) if usage_raw else 0
        ),
    }
    return content, usage


# ---------------------------------------------------------------------------
# Batched typing (K-G2) — N candidates per call, one verdict per candidate.
#
# The batch PROMPT, the idx correlation protocol, the parse-integrity accounting
# and the truncation salvage all live in ``relationship_typing_batch``; every
# verdict it returns has already been validated through THIS module's
# ``_coerce_typing``, so batching cannot loosen what production accepts. What
# lives here is only the run-loop policy: how a call that under-answers is
# recovered, and what that costs the run's counters.
#
# The imports are function-local: ``relationship_typing_batch`` imports
# ``_coerce_typing`` / ``ALLOWED_REL_TYPES`` from this module, so a module-level
# import here would close the cycle. Same precedent as the ``AnalystContext``
# import in the write path below.
# ---------------------------------------------------------------------------


async def _retry_single(
    llm: LLMHandlerLike,
    cand: Any,
    *,
    system_prompt: str,
    max_tokens: int,
    temperature: float,
    gate_text: str,
    usage_sink: dict[str, int],
) -> Any | None:
    """Re-ask ONE candidate the batch failed to answer, on the single-candidate
    path (:data:`_SYSTEM_PROMPT` + :func:`_build_user_prompt`).

    This is the "retry-or-skip" half of the batch contract: a model that drops,
    reorders or duplicates an ``idx`` costs that ONE candidate a second call —
    never the batch. Returns a verdict, or ``None`` to skip (counted
    ``degraded``). Measured parse integrity for the production typer is 100%
    over 17/17 calls, so this path is cold by design; it exists so that when it
    is not cold, the failure is bounded and counted rather than silent.
    """
    from .relationship_typing_batch import parse_batch_response

    user_prompt = _build_user_prompt(
        source=cand.source,
        target=cand.target,
        evidence_text=str(cand.evidence_text or ""),
        facts=list(cand.facts or ()),
        candidate_intermediaries=tuple(cand.intermediaries or ()),
    )
    try:
        raw, usage = await _type_via_llm(
            llm,
            user_prompt=user_prompt,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as exc:
        logger.warning(
            "relationship_reifier.retry_llm_failed pair=%s/%s err=%s",
            cand.source, cand.target, exc,
        )
        return None
    for k in usage_sink:
        usage_sink[k] += int(usage.get(k, 0) or 0)

    # Score the single response through the BATCH parser rather than
    # re-implementing verdict construction: a one-object response with no idx
    # hits its positional fallback (1 object, 1 candidate — unambiguous), and
    # the retry then inherits every rule the batch path has, including the
    # same-entity short-circuit, for free. Two verdict builders would be two
    # places for the rules to drift.
    result = parse_batch_response(
        raw, [cand], sports_gate_text={cand.idx: gate_text}
    )
    if not result.verdicts:
        logger.warning(
            "relationship_reifier.retry_parse_failed pair=%s/%s",
            cand.source, cand.target,
        )
        return None
    return result.verdicts[0]


async def _type_one_batch(
    llm: LLMHandlerLike,
    batch: Sequence[Any],
    *,
    batch_system_prompt: str,
    single_system_prompt: str,
    single_max_tokens: int,
    temperature: float,
    gate_text: Mapping[int, str],
    usage_sink: dict[str, int],
) -> tuple[list[Any], int]:
    """One batched typing call, then a bounded single-candidate retry for
    whatever it failed to answer. Returns ``(verdicts, degraded)``.

    **Never batch-abort.** A transport failure degrades the whole batch and the
    sweep continues (the module's degrade-not-drop rule, lifted to the batch
    level). A PARSE failure is not even that: the harness salvages every
    complete verdict object that preceded a truncation, so a partly-spent call
    keeps its answered candidates and only the unanswered ones are retried.
    """
    from .relationship_typing_batch import (
        build_batch_user_prompt,
        max_tokens_for_batch,
        parse_batch_response,
    )

    if not batch:
        return [], 0

    try:
        raw, usage = await _type_via_llm(
            llm,
            user_prompt=build_batch_user_prompt(batch),
            system_prompt=batch_system_prompt,
            max_tokens=max_tokens_for_batch(len(batch)),
            temperature=temperature,
        )
    except Exception as exc:
        logger.warning(
            "relationship_reifier.batch_llm_failed n=%d err=%s", len(batch), exc
        )
        return [], len(batch)
    for k in usage_sink:
        usage_sink[k] += int(usage.get(k, 0) or 0)

    result = parse_batch_response(raw, batch, sports_gate_text=dict(gate_text))
    verdicts = list(result.verdicts)
    if result.parse_ok:
        return verdicts, 0

    logger.warning(
        "relationship_reifier.batch_parse_degraded n=%d recovered=%d missing=%s "
        "unexpected=%s duplicate=%s truncated=%s",
        len(batch), result.recovered, result.missing_idx,
        result.unexpected_idx, result.duplicate_idx, result.truncated,
    )
    missing = set(result.missing_idx)
    if not missing:
        return verdicts, 0

    by_idx = {c.idx: c for c in batch}
    degraded = 0
    for idx in sorted(missing):
        cand = by_idx.get(idx)
        if cand is None:  # pragma: no cover - defensive
            degraded += 1
            continue
        verdict = await _retry_single(
            llm,
            cand,
            system_prompt=single_system_prompt,
            max_tokens=single_max_tokens,
            temperature=temperature,
            gate_text=str(gate_text.get(idx) or cand.evidence_text or ""),
            usage_sink=usage_sink,
        )
        if verdict is None:
            degraded += 1
        else:
            verdicts.append(verdict)
    return verdicts, degraded


async def _write_typed_nexus(
    pool: Any,
    payload: NexusPayload,
    *,
    derived: Sequence[UUID],
    actx: Any,
    keeper_cache: dict[str, str],
) -> str:
    """Side-write one typed nexus. Returns the outcome for the run counters:
    ``"written"`` / ``"superseded"`` / ``"self_loop"`` / ``"degraded"``.

    Extracted verbatim from the old per-candidate loop when typing went batched
    — the write is per-EDGE either way, and keeping it inline would have made
    the batch loop unreadable. The E1/N4 discipline is unchanged: canonicalize →
    resolve_keeper → self-loop gate → write.
    """
    try:
        async with pool.acquire() as conn:
            # E1 — CANONICALIZE-AT-WRITE: resolve each endpoint (already
            # surface-canonicalized + LLM-renamed by _coerce_typing) to its
            # elected entity_profiles KEEPER's canonical_name, so a fragment
            # ('SNSC', 'Resistance') or an alias converges onto the one graph
            # actor instead of minting a distinct node. resolve_keeper NEVER
            # raises + returns the input unchanged on any miss/error, so one bad
            # probe can't sink the row.
            new_subject = (
                await resolve_keeper(
                    conn, payload.subject, entity_class="entity",
                    cache=keeper_cache,
                )
            ).strip()
            new_object = (
                await resolve_keeper(
                    conn, payload.object, entity_class="entity",
                    cache=keeper_cache,
                )
            ).strip()
            # N4 — re-run the self-loop gate AFTER the keeper rewrite (the
            # ordering is canonicalize → resolve_keeper → self-loop → write).
            # Two surfaces that fold onto the SAME keeper ('Axis of Resistance' +
            # 'Resistance' → one keeper) are now identical strings, so
            # same_referent catches the self-loop that differing raw surfaces
            # hid. A degenerate rewrite (empty) is ignored — keep the pre-rewrite
            # endpoint rather than drop the edge.
            if new_subject and new_object:
                if same_referent(new_subject, new_object):
                    logger.info(
                        "relationship_reifier.keeper_self_loop dropped "
                        "pair=%s/%s -> %s/%s",
                        payload.subject, payload.object, new_subject, new_object,
                    )
                    return "self_loop"
                if (new_subject, new_object) != (payload.subject, payload.object):
                    payload.subject = new_subject
                    payload.object = new_object
                    # Keep the human label consistent with the rewritten
                    # endpoints (label was built from the pre-keeper surfaces in
                    # _coerce_typing).
                    payload.label = (
                        f"{new_subject} {payload.rel_type} {new_object}"[:4096]
                    )
            before = await conn.fetchval(
                "SELECT count(*) FROM nexuses "
                "WHERE lower(subject)=lower($1) AND lower(object)=lower($2) "
                "AND superseded_by IS NOT NULL",
                payload.subject, payload.object,
            )
            out, dlq = await write_nexus(
                conn,
                analyst_ctx=actx,
                payload=payload,
                derived_from=list(derived),
                source_signal_ids=list(derived),  # D15: populate BOTH columns
            )
            if out is not None:
                after = await conn.fetchval(
                    "SELECT count(*) FROM nexuses "
                    "WHERE lower(subject)=lower($1) AND lower(object)=lower($2) "
                    "AND superseded_by IS NOT NULL",
                    payload.subject, payload.object,
                )
                return "superseded" if (after or 0) > (before or 0) else "written"
            if dlq is not None:
                return "degraded"
            return "degraded"
    except Exception as exc:
        logger.warning(
            "relationship_reifier.write_failed pair=%s/%s err=%s",
            payload.subject, payload.object, exc,
        )
        return "degraded"


def _opt_int(options: Mapping[str, Any], key: str, default: int) -> int:
    """A descriptor-set integer knob, or ``default``. Never raises — the option
    plane already validated the value; this is the last-mile coercion."""
    try:
        return int(options[key])
    except (KeyError, TypeError, ValueError):
        return int(default)


def _opt_float(options: Mapping[str, Any], key: str, default: float) -> float:
    try:
        return float(options[key])
    except (KeyError, TypeError, ValueError):
        return float(default)


# ---------------------------------------------------------------------------
# Public entry — run_method
# ---------------------------------------------------------------------------


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: ReifierDeps | LLMHandlerLike,
) -> AnalystMethodResult:
    """Execute one ``relationship_reifier`` sweep.

    ``deps`` accepts a :class:`ReifierDeps` (production) or a bare
    :class:`LLMHandlerLike` (the back-compat test path — pg-less, types only
    the rows passed in ``inputs``). Returns an :class:`AnalystMethodResult`
    whose ``finding`` is the per-run summary; the nexus rows are side-written.
    """
    if not isinstance(deps, ReifierDeps):
        deps = ReifierDeps(llm=deps)

    analyst_id = str(options.get("analyst_id") or KIND_NAME)
    target_id = options.get("target_id")
    run_id = options.get("run_id")
    if isinstance(run_id, str):
        try:
            run_id = UUID(run_id)
        except ValueError:
            run_id = None
    system_prompt = deps.system_prompt or _SYSTEM_PROMPT
    now = datetime.now(tz=timezone.utc)

    # X-1 / QW1-B — descriptor-declared knobs. The runtime merges the
    # descriptor's ``method.options`` into ``options`` (validated against
    # ``handler_options.ANALYST_KIND_OPTIONS['relationship_reifier']``, with a
    # receipt on the run trace for anything it dropped). The deps value is the
    # fallback, so an options-less descriptor is byte-identical to the dataclass
    # defaults.
    max_candidates = _opt_int(options, "max_candidates", deps.max_candidates)
    batch_size = max(1, _opt_int(options, "batch_size", deps.batch_size))
    qualification_bar = _opt_float(
        options, "qualification_bar", deps.qualification_bar
    )
    min_independent_sources = _opt_int(
        options, "min_independent_sources", deps.min_independent_sources
    )

    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    candidates: list[dict[str, Any]] = []
    pool = deps.pg_pool
    # E1 — one keeper-election memo for the WHOLE run. Selection resolves every
    # examined pair and the write path resolves every typed one; sharing the memo
    # means each surface costs one probe per run, not two.
    keeper_cache: dict[str, str] = {}
    selection = SelectionCounters()

    # 1) Assemble candidate pairs. Prefer the live proposed_edges sweep; fall
    #    back to the inputs the runtime materialized (test / no-pool path).
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                candidates, selection = await select_candidates(
                    conn,
                    limit=max_candidates,
                    bar=qualification_bar,
                    min_sources=min_independent_sources,
                    keeper_cache=keeper_cache,
                )
        except Exception as exc:
            logger.warning("relationship_reifier.read_candidates_failed err=%s", exc)
            candidates = []
    if not candidates:
        for row in inputs[:max_candidates]:
            src = row.get("source_entity") or row.get("subject") or row.get("src")
            tgt = row.get("target_entity") or row.get("object") or row.get("dst")
            if src and tgt:
                candidates.append({
                    "source_entity": str(src),
                    "target_entity": str(tgt),
                    "evidence_text": str(row.get("evidence_text") or ""),
                    "produced_at": row.get("produced_at") or now,
                    "derived_from": list(row.get("derived_from") or []),
                })

    from .relationship_typing_batch import BATCH_SYSTEM_PROMPT, BatchCandidate

    n_candidates = len(candidates)
    typed = 0
    accepted = 0
    rejected = 0
    alias_pairs = 0
    alias_pairs_routed = 0
    written = 0
    superseded = 0
    degraded = 0
    # D3: candidate pairs dropped as junk / demonym self-loop. Selection already
    # dropped these for the pg path (its own counter); this one covers the
    # no-pool ``inputs`` path, which bypasses selection entirely.
    skipped_endpoints = selection.skipped_endpoints
    budget_paused = False
    batch_system_prompt = deps.system_prompt or BATCH_SYSTEM_PROMPT

    from ..provenance import AnalystContext  # local import — avoid cycle

    actx = AnalystContext(
        analyst_id=analyst_id,
        analyst_version=str(options.get("analyst_version") or ""),
        run_id=run_id if isinstance(run_id, UUID) else None,  # type: ignore[arg-type]
        target_id=target_id,
        target_version=options.get("target_version"),
    )

    # 2) TYPE. K-G2: N candidates per LLM call, one verdict per candidate,
    #    correlated by idx (never by position). One typer — no escalation ladder;
    #    see SINGLE_TYPER_RATIONALE.
    for start in range(0, len(candidates), batch_size):
        chunk = candidates[start : start + batch_size]

        # Honor the budget envelope before each LLM call (degrade-not-drop:
        # stop issuing new calls, keep what we already wrote).
        if deps.budget is not None:
            try:
                envelope = await deps.budget.check_envelope()
            except Exception:  # pragma: no cover - defensive
                envelope = "ok"
            if envelope != "ok":
                budget_paused = True
                break

        batch: list[BatchCandidate] = []
        gate_text: dict[int, str] = {}
        by_idx: dict[int, dict[str, Any]] = {}
        for offset, cand in enumerate(chunk):
            raw_source = str(cand["source_entity"])
            raw_target = str(cand["target_entity"])
            # D3 EARLY GATE — drop junk endpoints and canonicalize the pair
            # BEFORE it costs a slot in a typing call. A demonym pair ("Iran" /
            # "Iranian") both canonicalize to "Iran" → a self-loop, never a
            # relationship; a junk endpoint ("TV") is dropped. Selection already
            # applies this for the pg path; it stays here because the no-pool
            # ``inputs`` path bypasses selection entirely, and because
            # _coerce_typing still re-guards the LLM's own subject/object.
            if is_junk_entity(raw_source) or is_junk_entity(raw_target):
                skipped_endpoints += 1
                continue
            c_source, _ = canonicalize_entity(raw_source, "entity")
            c_target, _ = canonicalize_entity(raw_target, "entity")
            # DQ M8 — same_referent (not a bare lower() equality) also drops a
            # plain singular/plural self-loop ("Houthi"/"Houthis").
            if not c_source or not c_target or same_referent(c_source, c_target):
                skipped_endpoints += 1
                continue

            facts_ctx: list[dict[str, Any]] = []
            intermediaries: list[str] = []
            if pool is not None:
                try:
                    async with pool.acquire() as conn:
                        facts_ctx = await _recent_facts_for(
                            conn, source=c_source, target=c_target
                        )
                        # 3-entity proxy-chain path (#99): only for the more-
                        # corroborated pairs (cost guard), offer the third
                        # entities co-mentioned with BOTH endpoints so the typer
                        # SELECTS a real cut-out instead of hallucinating one.
                        try:
                            pair_conf = float(cand.get("confidence") or 0.0)
                        except (TypeError, ValueError):
                            pair_conf = 0.0
                        if pair_conf >= MIN_INTERMEDIARY_PAIR_CONFIDENCE:
                            intermediaries = await _intermediary_candidates_for(
                                conn,
                                source=c_source,
                                target=c_target,
                                limit=MAX_INTERMEDIARY_CANDIDATES,
                            )
                except Exception:  # pragma: no cover - context is best-effort
                    facts_ctx = []
                    intermediaries = []

            idx = start + offset
            by_idx[idx] = cand
            # FU4 — the gate runs over the UNION of the excerpt + all backing
            # source-signal texts, so a sports frame in a CO-SOURCE signal (not
            # the excerpt) still gates. The PROMPT keeps the terse excerpt.
            gate_text[idx] = _sports_gate_text(cand)
            batch.append(
                BatchCandidate(
                    idx=idx,
                    source=c_source,
                    target=c_target,
                    evidence_text=str(cand.get("evidence_text") or ""),
                    facts=tuple(facts_ctx),
                    intermediaries=tuple(intermediaries),
                    ref=idx,
                )
            )

        if not batch:
            continue

        verdicts, batch_degraded = await _type_one_batch(
            deps.llm,
            batch,
            batch_system_prompt=batch_system_prompt,
            single_system_prompt=system_prompt,
            single_max_tokens=deps.max_tokens,
            temperature=deps.temperature,
            gate_text=gate_text,
            usage_sink=total_usage,
        )
        degraded += batch_degraded
        typed += len(verdicts)

        # 3) Side-write one nexus per ACCEPTED verdict (write_nexus supersedes a
        #    prior open row on polarity/label change). Degrade-not-drop.
        for verdict in verdicts:
            # ALIAS PAIR — "these are two names for one entity". Never an edge
            # (an entity is not related to itself), and not a plain rejection
            # either: it is a merge-candidate signal. See reifier_alias_pairs.
            if getattr(verdict, "same_entity", False):
                alias_pairs += 1
                if pool is not None:
                    async with pool.acquire() as conn:
                        outcome = await record_alias_pair(
                            conn, verdict.source, verdict.target,
                            confidence=verdict.confidence,
                            keeper_cache=keeper_cache,
                        )
                    if outcome == "recorded":
                        alias_pairs_routed += 1
                continue
            if not verdict.accepted or verdict.payload is None:
                rejected += 1
                continue
            accepted += 1
            cand = by_idx.get(int(verdict.idx))
            if cand is None:  # pragma: no cover - defensive
                continue
            payload = verdict.payload
            # event time = the pair's produced_at (the co-mention's event
            # clock), else now. Mirrors fact_extractor stamping valid_from at
            # event time.
            ev = cand.get("produced_at")
            payload.valid_from = ev if isinstance(ev, datetime) else now
            # D15 — carry the originating signal UUIDs (the co-occurrence edge's
            # derived_from) so the nexus lands with real provenance.
            derived = [
                u for u in (cand.get("derived_from") or []) if isinstance(u, UUID)
            ]
            payload.source_signal_ids = list(derived)

            if pool is None:
                # No-pool test path: the typing was counted; without a pool there
                # is nothing to persist.
                continue
            outcome = await _write_typed_nexus(
                pool, payload, derived=derived, actx=actx, keeper_cache=keeper_cache
            )
            if outcome == "written":
                written += 1
            elif outcome == "superseded":
                written += 1
                superseded += 1
            elif outcome == "degraded":
                degraded += 1

    finding = _build_summary(
        n_candidates=n_candidates,
        typed=typed,
        accepted=accepted,
        rejected=rejected,
        alias_pairs=alias_pairs,
        alias_pairs_routed=alias_pairs_routed,
        written=written,
        superseded=superseded,
        degraded=degraded,
        skipped_endpoints=skipped_endpoints,
        budget_paused=budget_paused,
        target_id=target_id,
        selection=selection,
    )
    return AnalystMethodResult(finding=finding, usage=total_usage)


def _build_summary(
    *,
    n_candidates: int,
    typed: int,
    written: int,
    superseded: int,
    degraded: int,
    skipped_endpoints: int = 0,
    budget_paused: bool,
    target_id: str | None,
    accepted: int = 0,
    rejected: int = 0,
    alias_pairs: int = 0,
    alias_pairs_routed: int = 0,
    selection: SelectionCounters | None = None,
) -> FindingPayload:
    # K-G2 counter vocabulary. ``typed`` is now "the typer returned a verdict",
    # which splits into ``accepted`` (a payload the coercion accepted → an edge
    # is attempted) and ``rejected`` (the model said no relationship, or the
    # verdict failed validation). Before batching, ``typed`` meant only the
    # accepted half, so a run could not distinguish "the typer rejected these"
    # from "the typer never saw these" — the exact ambiguity that let the dead-row
    # window hide for weeks.
    title = (
        f"Relationship reifier: {written} nexuses written "
        f"({typed} typed / {n_candidates} candidates)"
    )
    if target_id:
        title = f"{title} for {target_id}"
    tags = ["meta", "relationship_reifier"]
    if written:
        tags.append("nexuses_written")
    if degraded:
        tags.append("degraded")
    if budget_paused:
        tags.append("budget_paused")
    # K-G2 — the selection receipt. The old summary said ``candidates=40`` on
    # every tick while all 40 were dead rows; these counters are what makes a
    # collapsed window visible without a DB session.
    sel = (selection or SelectionCounters()).as_dict()
    return FindingPayload(
        title=title[:2048],
        body=(
            f"candidates={n_candidates} typed={typed} accepted={accepted} "
            f"rejected={rejected} alias_pairs={alias_pairs} "
            f"alias_pairs_routed={alias_pairs_routed} written={written} "
            f"superseded={superseded} degraded={degraded} "
            f"skipped_endpoints={skipped_endpoints} "
            f"budget_paused={budget_paused} "
            + " ".join(f"selection_{k}={v}" for k, v in sel.items())
        )[:65536],
        confidence=1.0,
        tags=tags,
        data={
            "meta": True,
            "sub_handler": "relationship_reifier",
            "candidates": n_candidates,
            "typed": typed,
            "accepted": accepted,
            "rejected": rejected,
            "alias_pairs": alias_pairs,
            "alias_pairs_routed": alias_pairs_routed,
            "written": written,
            "superseded": superseded,
            "degraded": degraded,
            "skipped_endpoints": skipped_endpoints,
            "budget_paused": budget_paused,
            "selection": sel,
        },
    )


__all__ = [
    "KIND_NAME",
    "OUTPUT_KIND",
    "ReifierDeps",
    "run_method",
    "ALLOWED_REL_TYPES",
    "DEFAULT_BATCH_SIZE",
    "SINGLE_TYPER_RATIONALE",
    # Re-exported: a SELECTION knob that now lives with the selection
    # (:mod:`.reifier_selection`), kept importable here because this was its
    # name first and the K-G2 report cites it at this path.
    "MIN_EDGE_CONFIDENCE",
]
