# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""journal_assessor — Legba's first-person reflective voice (plan §4).

The 11th OutputKind's producer. The ONE analyst pointed at the whole organism,
including itself: it reads what the other analysts concluded and narrates a
coherent point of view OVER all of it, in its own voice. It emits exactly one
``JournalPayload`` (entry | consolidation) into the dedicated ``journal_entries``
table — OFF the fact/finding/nexus chain (§3.1). It must NEVER write a fact,
nexus, finding, situation, or hypothesis.

ENGINE (plan §4.1, a hard commitment): the in-actor ``llm_planner`` /
``inline_target``-family envelope — NOT the ``deep_consult`` Dapr workflow (which
hardcodes OUTPUT_KIND = FINDING and rides the known-broken long-activity
round-trip, task #86). This module reuses inline_target's proven GATHER + reason
building blocks (``_gather`` / ``_reason_via_llm``) but:

  * threads the JOURNAL persona (legba.prompts.journal_assessor:JOURNAL_SYSTEM)
    as the system prompt on EVERY LLM call — and authors it WITHOUT the stock
    ``with_preamble`` JSON-only / BLUF anti-voice (§4.2, the headline fix);
  * emits a ``JournalPayload`` (parsing inline [[ref:<uuid>]] citation markers
    into the per-claim ``claims`` binding + the flat ``cited_substrate_refs``);
  * returns ``derived_from=[]`` — the direction-asymmetric off-chain node (§3.5).

WAVE 1 (plan §5 / §4.3 / §4.4 / §10 / §12). The staged arc is now full:

  PLAN ──► GATHER (deep ReAct over the journal_read pack — the WHOLE ANIMAL +
           its OWN instruments, §5) ──► FIELD-NOTES SEAM (§4.3, the ONE
           separation: still in persona, rewrite the gathered observations into
           rich, cited prose — dropping ONLY the raw tool JSON — so NARRATE sees
           the whole system, not a thin summary; this REPLACES deep_consult's
           ``_evidence_brief``) ──► NARRATE (§4.4, tools stay LIVE: a small ReAct
           loop around the narrate call lets the agent pull ONE more thread
           mid-entry) ──► REFLECT (§10, permissive per-claim citation flag —
           flag/speculation-mark an uncited factual span, NEVER strip; a
           perspective sentence is exempt) ──► HONESTY (§10, the DETERMINISTIC
           ``honesty_flags`` post-step forced from the actual calibration metrics
           in the substrate, regardless of what the model wrote).

READ_SLICE stays ``None`` — the default META global signal slice primes the run
and ``get_journal_delta`` (in GATHER) tells the agent what changed since the last
entry. The delta-priming READ_SLICE is a later refinement.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID

from ...runtime.substrate_query_port import _ASSESSMENT_PRODUCER_ANALYSTS
from ..provenance.consumption import CONSUMPTION_CONTEXT_JOURNAL
from ..provenance.kinds import OutputKind
from ..provenance.models import JournalClaim, JournalPayload
from .agency.journal_propose import JOURNAL_PROPOSE_TOOLS
from .agency.journal_read import JOURNAL_READ_TOOLS
from .consult_on_demand import _bounded_tool_json
from .inline_target import (
    AnalystMethodResult,
    InlineTargetDeps,
    LLMHandlerLike,
    _gather,
    _normalize_citation_markers,
    _reason_via_llm,
    _resolve_signal_id,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Host-discovered constants (plan §4.8 leg 1)
# ---------------------------------------------------------------------------

KIND_NAME = "journal_assessor"
SCHEMA_VERSION = "legba/analyst.journal_assessor/1-0-0"
HANDLER_VERSION = "0.3.0"
PROMPT_MODULE_PATH = "legba.prompts.journal_assessor:JOURNAL_SYSTEM"

# Wave 2 (plan §4.7 / §12): the consolidation tier is the SAME kind
# (``identity.kind: journal_assessor``) with a DISTINCT ``identity.id``
# (``journal_consolidator``). "There is no ``mode=consolidation`` option on a
# single descriptor — the tier is the descriptor." So this one ``run_method``
# selects the journal ``entry_kind`` purely from the analyst id it is running as:
# the consolidator id → ``consolidation`` (which makes the write path fire
# ``supersede_prior_consolidation``); every other id → ``entry`` (pure append).
CONSOLIDATOR_ANALYST_ID = "journal_consolidator"
CONSOLIDATOR_PROMPT_MODULE_PATH = (
    "legba.prompts.journal_consolidator:CONSOLIDATOR_SYSTEM"
)

# Chronicle tier (2026-07-21, planning/CHRONICLE_BUILD_2026-07-21.md): the
# public-record tier — The Legba Report produced inside the platform. THIRD
# analyst id on the same kind: detached third-person cited prose over the
# verified tower top, weekly beat, append-only (never supersedes), V1 verify
# gates every entry via the shared kind, NO publish edge (the Ghost sink is
# deliberately absent).
CHRONICLE_ANALYST_ID = "chronicle_assessor"
CHRONICLE_PROMPT_MODULE_PATH = (
    "legba.prompts.chronicle_assessor:CHRONICLE_SYSTEM"
)

# VOICES LV-1 (2026-07-21, planning/VOICES_PLAN_2026-07-21.md, DL-1/DL-3): the
# faculty-lens tier. FOUR function-typed analyst ids on the SAME journal_assessor
# kind — each reads the VERIFIED tower top through ONE declared falsifiable prior
# (living in its persona module; the descriptor content hash IS the prior version,
# DL-2) and writes an interpretive read into journal_entries (entry_kind='lens').
# A FIFTH id (lens_diff) is the chorus DIFF pass: it reads the four reads and
# narrates agree/split/outlier (entry_kind='lens_diff'), never merging them. All
# five are append-only (never supersede), all ride V1 verify via the shared kind,
# none has a publish sink.
LENS_ANALYST_IDS: tuple[str, ...] = (
    "lens_trend", "lens_baserate", "lens_capability", "lens_intent",
)
# Each faculty module exports the SAME three names (LENS_SYSTEM / LENS_PRIOR_BLOCK
# / LENS_ID), so the descriptor's prompt_module string alone selects the persona
# — no per-faculty branch. The prior block is imported directly by run_method
# (persona-module-only wiring, §2.4 — NO options-contract / dapr_actors change).
LENS_PROMPT_MODULE_PATHS: dict[str, str] = {
    aid: f"legba.prompts.{aid}:LENS_SYSTEM" for aid in LENS_ANALYST_IDS
}
LENS_DIFF_ANALYST_ID = "lens_diff"
LENS_DIFF_PROMPT_MODULE_PATH = "legba.prompts.lens_diff:LENS_DIFF_SYSTEM"


def _lens_prior_block_for(analyst_id: str | None) -> str:
    """Resolve a faculty's declared prior block from its persona module (§2.4).

    The prior lives in the descriptor body via the persona module (DL-2); this
    imports the module's ``LENS_PRIOR_BLOCK`` so ``run_method`` can echo it into
    the user prompt VERBATIM — the copy the verify judge / audit points at as
    "the prior this run actually saw," independent of persona wiring. Returns ""
    for a non-faculty id (the diff pass has no prior of its own) or on an import
    miss (degrade-not-crash — the persona module still carries the prior)."""
    if analyst_id not in LENS_ANALYST_IDS:
        return ""
    try:
        import importlib

        module = importlib.import_module(f"legba.prompts.{analyst_id}")
        block = getattr(module, "LENS_PRIOR_BLOCK", "")
        return block if isinstance(block, str) else ""
    except Exception as exc:  # degrade-not-drop — persona module still has it
        logger.warning(
            "journal_assessor.lens_prior.import_failed id=%s err=%s",
            analyst_id, exc,
        )
        return ""


def _entry_kind_for_analyst(analyst_id: str | None) -> str:
    """Select the journal ``entry_kind`` from the running analyst id (plan §4.7).

    The tiers share the kind module; the discriminator is the descriptor id,
    NOT a per-descriptor mode flag. The consolidator id distills into a
    ``consolidation`` (supersession-carrying); the chronicle id into a
    ``chronicle`` (the public-record tier, pure append); a lens faculty id into a
    ``lens`` and the chorus diff id into a ``lens_diff`` (the VOICES tier, pure
    append — DL-1; lens_diff gets its OWN kind because its payload/consumption
    differ from a single faculty's read, same logic as consolidation ≠ entry);
    any other id (the entry tier ``journal_assessor``) appends an ``entry``.
    """
    if analyst_id == CONSOLIDATOR_ANALYST_ID:
        return "consolidation"
    if analyst_id == CHRONICLE_ANALYST_ID:
        return "chronicle"
    if analyst_id == LENS_DIFF_ANALYST_ID:
        return "lens_diff"
    if analyst_id in LENS_ANALYST_IDS:
        return "lens"
    return "entry"


# The 11th OutputKind — the host's analyst-output dispatcher writes a
# ``journal_entries`` row (NOT a finding). This is what makes the kind off-chain.
OUTPUT_KIND: OutputKind = OutputKind.JOURNAL

# READ_SLICE = None (Wave 1) — the host's default META reader yields the global
# 24h signal slice that primes the reflection; deep investigation (incl.
# get_journal_delta "what changed since last entry") is in GATHER. The delta
# priming reader is a later refinement; if a custom reader is built, init all
# slice-locals BEFORE any `if target_filter:` branch (this is a META analyst,
# target_filter=None — the 5c scope_predicate UnboundLocalError class).
READ_SLICE = None

# The hard ceiling on the tools-live NARRATE ReAct loop (§4.4). One extra thread
# is the design intent ("pull ONE more thread mid-entry"); the cap bounds it so a
# runaway narrate can't grind the cadence tick. The GATHER round cap
# (method.gather.max_rounds, clamped ≤6) governs the deep investigation loop
# separately.
_NARRATE_MAX_TOOL_ROUNDS = 2


# Inline citation marker the body carries; the UI resolves it to a chip at the
# cited span. We also harvest the UUIDs into claims + cited_substrate_refs.
_REF_MARKER_RE = re.compile(r"\[\[ref:([0-9a-fA-F-]{36})\]\]")
# An explicit speculation / perspective marker the agent may use in lieu of a ref
# on a factual-sounding span (§4.5 / §10) — kept, never stripped.
_SPECULATION_RE = re.compile(
    r"\[\[(?:spec|speculation|perspective|wonder|inference|unverified|instrument)\]\]",
    re.IGNORECASE,
)
# The [[instrument]] marker specifically (V2): exempt like the spec family, BUT
# guarded — an instrument span is about the SELF; one carrying world proper
# nouns is a citation dodge (review A) and downgrades to an uncited fact claim.
_INSTRUMENT_MARKER_RE = re.compile(r"\[\[instrument\]\]", re.IGNORECASE)
_SELF_TERMS = frozenset({
    "brier", "bss", "betweenness", "centrality", "triad", "triads", "graph",
    "feed", "feeds", "source", "sources", "budget", "run", "runs", "critic",
    "calibration", "salience", "faithfulness", "intensity", "poll", "payload",
    "postgres", "qdrant", "nats", "legba", "instrument", "pipeline", "cadence",
})
_CAPWORD_RE = re.compile(r"(?<!^)(?<![.!?]\s)\b([A-Z][a-zA-Z]{2,})")


def _instrument_span_is_worldly(span: str) -> bool:
    """Review-A guard: an ``[[instrument]]`` span carrying >=2 mid-sentence
    capitalized words that are NOT self-vocabulary reads as a WORLD claim
    wearing the exemption — treat it as an uncited fact, never exempt it."""
    hits = [w for w in _CAPWORD_RE.findall(span) if w.lower() not in _SELF_TERMS]
    return len(hits) >= 2
# A markdown title line ("# ...") the model may lead with.
_TITLE_LINE_RE = re.compile(r"^\s*#+\s*(.+?)\s*$", re.MULTILINE)
# T-4(c): a leading BOLD-ONLY line ("**Trump Signals …**") — the shape the
# chronicle tier emits as its title. It must be preferred over the first ATX
# section header (else "### The Gate" wins over the real headline). Matches only a
# line that is ENTIRELY one bold span (optional leading whitespace), so a bold word
# mid-prose is never mistaken for a title.
_BOLD_TITLE_RE = re.compile(r"^\s*\*\*(.+?)\*\*\s*$", re.MULTILINE)
_MAX_TITLE_CHARS = 240

# A coarse "this span asserts a fact" heuristic for the permissive REFLECT flag:
# a span with a number, a date, a proper-noun-ish capitalized token, or a
# declarative copula reads as factual. This is INTENTIONALLY permissive — it only
# FLAGS (never deletes), and the tie-breaker is voice-preservation (§4.5): when in
# doubt we treat the span as perspective and leave it alone.
_FACTUAL_HINT_RE = re.compile(
    r"\d|\b(?:is|are|was|were|has|have|flipped|rose|fell|went|"
    r"quiet|spiked|dropped|increased|decreased)\b",
    re.IGNORECASE,
)
# First-person wonder/inference cues mark a span as PERSPECTIVE (exempt) even if
# it carries a factual-looking hint — the connective/wondering tissue that IS the
# voice (the historical metaphor-ban pole we explicitly do NOT recreate).
_PERSPECTIVE_CUE_RE = re.compile(
    r"\b(?:I |I'm|I've|I wonder|it (?:makes|feels|seems)|"
    r"uneasy|curious|strikes me|reminds me|maybe|perhaps|"
    r"my sense|I suspect|I think|it worries me)\b",
    re.IGNORECASE,
)


def build_prompt_module() -> Any:
    """The journal runs on the in-actor envelope, not the GEPA compile surface,
    so it has no DSPy module. Returning the persona STRING keeps the discovery
    contract uniform (callers that introspect ``build_prompt_module`` get the
    voice). The system prompt is threaded by ``run_method`` directly.
    """
    from legba.prompts.journal_assessor import JOURNAL_SYSTEM
    return JOURNAL_SYSTEM


# ---------------------------------------------------------------------------
# Claim extraction + the permissive REFLECT citation flag (§3.6 / §4.5 / §10)
# ---------------------------------------------------------------------------


def _span_is_factual(text: str) -> bool:
    """Coarse, PERMISSIVE fact-vs-perspective classifier for the REFLECT flag.

    Returns True only when a span reads as a factual assertion AND carries no
    first-person wonder/inference cue. The tie-breaker is voice-preservation
    (§4.5): a span that hints at both fact and perspective is treated as
    perspective (exempt) — we never want to flag the connective tissue that IS
    the voice. This NEVER deletes anything; the worst it does is attach a
    ``needs_citation`` marker the UI renders distinctly (§9).
    """
    t = text.strip()
    if not t:
        return False
    if _PERSPECTIVE_CUE_RE.search(t):
        return False
    return bool(_FACTUAL_HINT_RE.search(t))


def _reflect_claims(body: str) -> tuple[list[JournalClaim], list[UUID], list[str]]:
    """Parse the body into per-claim citation bindings, FLAGGING (never stripping)
    an uncited factual span (plan §3.6 / §4.5 / §10 — the REFLECT pass).

    For each span (split on blank lines):
      * a span with ≥1 ``[[ref:<uuid>]]`` → a ``kind='fact'`` claim bound to its
        refs (a cited factual claim survives intact);
      * a span with no ref but an explicit speculation marker (``[[spec]]`` …) OR
        no factual hint → a ``kind='perspective'`` claim (wonder/inference is
        honest without a UUID — the perspective sentence is EXEMPT);
      * a span that reads factual but carries NO ref and NO speculation marker →
        a ``kind='fact'`` claim with empty refs, tagged ``needs_citation`` in its
        text (FLAGGED, never deleted — voice-preservation is the tie-breaker; the
        UI renders it in the "unverified perspective" style, §9).

    Returns ``(claims, flat_cited_refs, reflect_flags)`` where ``reflect_flags``
    is a per-run audit list (e.g. ``["uncited_factual_span"]``) the trace records.
    """
    flat: list[UUID] = []
    seen: set[UUID] = set()
    claims: list[JournalClaim] = []
    reflect_flags: list[str] = []
    for span in re.split(r"\n\s*\n", body):
        text = span.strip()
        if not text:
            continue
        span_refs: list[UUID] = []
        for m in _REF_MARKER_RE.finditer(span):
            try:
                u = UUID(m.group(1))
            except ValueError:
                continue
            span_refs.append(u)
            if u not in seen:
                seen.add(u)
                flat.append(u)
        if span_refs:
            # A cited factual claim — survives REFLECT intact.
            claims.append(
                JournalClaim(text_span=text[:8192], refs=span_refs, kind="fact")
            )
            continue
        has_spec_marker = bool(_SPECULATION_RE.search(span))
        is_instrument_marked = bool(_INSTRUMENT_MARKER_RE.search(span))
        # Review-A guard: [[instrument]] wearing world facts is a citation
        # dodge — strip the EXEMPTION (never the marker/text) and let the span
        # fall through as an uncited fact claim ([needs_citation]-flagged).
        if (
            has_spec_marker
            and is_instrument_marked
            and _instrument_span_is_worldly(span)
        ):
            has_spec_marker = False
            reflect_flags.append("instrument_marker_on_world_span")
        if has_spec_marker or not _span_is_factual(text):
            # Perspective / wonder / inference — EXEMPT (no ref required).
            # T-4(d): an honest [[instrument]] read (a legitimate self-metric with
            # no citable row) is a DISTINCT claim shape from ordinary perspective —
            # it is a self-fact stated without a ref, not wonder. JournalClaim
            # forbids extra fields (extra='forbid') and `kind` is a closed Literal,
            # so rather than force a schema/kind change (which would ripple to the
            # verify doc builder + the read-only journal API), we record the
            # distinction in the reflect audit (surfaced in the run trace/summary):
            # an 'instrument_perspective_span' entry marks that this exempt span was
            # an instrument read, not free-text perspective. The claim itself stays
            # kind='perspective' (the honest minimal representation).
            if is_instrument_marked and has_spec_marker:
                reflect_flags.append("instrument_perspective_span")
            claims.append(
                JournalClaim(text_span=text[:8192], refs=[], kind="perspective")
            )
            continue
        # Uncited factual span: FLAG, do NOT delete (voice-preservation §4.5).
        reflect_flags.append("uncited_factual_span")
        flagged = f"[needs_citation] {text}"
        claims.append(
            JournalClaim(text_span=flagged[:8192], refs=[], kind="fact")
        )
    return claims, flat, reflect_flags


# ---------------------------------------------------------------------------
# V4 — the GATHER [N] → journal [[ref:uuid]] bridge (MASTER_PLAN 2026-07-10)
# ---------------------------------------------------------------------------
# A bare ordinal citation marker ``[N]`` (1-3 digits). The journal narrates in
# [[ref:<uuid>]] and never renders a slice-[N], so an [N] the model wrote can
# ONLY come from a GATHER-gathered corpus doc's numbered preamble block
# (inline_target._gather's ``gathered_blocks``). This is DISTINCT from the
# double-bracket [[ref:...]] shape — a [[ref:uuid]] never matches this (the inner
# text is a uuid, not digits). We resolve each [N] against the run's
# citation_extension and rewrite the resolvable ones so _reflect_claims (which
# only sees [[ref:uuid]]) picks them up as real citations instead of dropping
# them on the floor at render (J4).
_GATHERED_ORDINAL_RE = re.compile(r"\[(\d{1,3})\]")


def _rewrite_gathered_citations(
    body: str,
    citation_extension: Mapping[int, Mapping[str, Any]],
) -> tuple[str, int]:
    """Rewrite GATHER-corpus ``[N]`` markers to durable ``[[ref:<uuid>]]``.

    ``citation_extension`` is inline_target ``_gather``'s ``{N -> citation entry}``
    map for the corpus documents GATHER numbered ([N]-citable). For each ``[N]``
    in the body whose ``N`` resolves to an extension entry carrying a valid signal
    uuid, replace the marker with ``[[ref:<uuid>]]`` so the journal's REFLECT pass
    binds it as a ``kind='fact'`` claim (otherwise the [N] grounds against nothing
    and the citation is lost at render — the J4 defect).

    NON-fabricating + surgical:
      * An ``[N]`` with no extension entry (or an entry with no resolvable uuid)
        is LEFT UNTOUCHED — never invent a ref for an unmapped ordinal.
      * The [[ref:uuid]] shape the journal already emits never matches
        ``_GATHERED_ORDINAL_RE`` (its inner text is a uuid, not digits), so the
        native slice/priming citation path is byte-for-byte unchanged.
      * Idempotent on a body with no gathered ordinals or an empty extension.

    Full-width bracket variants (``【N】`` / ``［N］``) the core plane emits
    non-deterministically are normalized to ASCII ``[N]`` FIRST (the
    fullwidth-bracket lesson), so a variant-bracketed gathered marker still
    rewrites rather than silently dropping.

    Returns ``(rewritten_body, rewritten_count)``.
    """
    if not body or not citation_extension:
        return body, 0
    text = _normalize_citation_markers(body)
    rewritten = 0

    def _sub(match: "re.Match[str]") -> str:
        nonlocal rewritten
        n = int(match.group(1))
        entry = citation_extension.get(n)
        if not isinstance(entry, Mapping):
            return match.group(0)  # unmapped ordinal — leave it, never fabricate
        sig = _resolve_signal_id(entry.get("signal_id"))
        if sig is None:
            return match.group(0)  # no resolvable uuid — leave it
        rewritten += 1
        return f"[[ref:{sig}]]"

    return _GATHERED_ORDINAL_RE.sub(_sub, text), rewritten


# V1 (journal verify profile) — cap the verify document so a sprawling entry
# can't blow the judge's context; 40 cited fact claims is far above a normal
# entry's count, so the cap is a backstop, not a working limit.
_JOURNAL_VERIFY_MAX_CLAIMS = 40


def build_journal_verify_inputs(payload: Any) -> tuple[str, list[str]]:
    """V1 — build the CITED-FACT-ONLY verify document for a journal entry.

    From the entry's ``claims`` sidecar keep only ``kind='fact'`` claims that
    carry >=1 ref. The §10 contract holds by construction: ``perspective``
    claims are EXEMPT (never judged, never stripped — they simply do not enter
    the document), and an UNCITED fact claim is already ``[needs_citation]``-
    flagged by REFLECT, so it is not double-tried here. Each kept claim's
    inline ``[[ref:<uuid>]]`` markers are rewritten to the ordinal ``[N]`` form
    the faithfulness floor + judge bind on; a ref listed on the claim but
    missing as an in-span marker (defensive) gets a trailing ``[N]`` so the
    floor still sees its support.

    Returns ``(doc, ordered_refs)`` where ``ordered_refs[N-1]`` is the uuid
    string behind marker ``[N]`` (the caller resolves those into the citation
    bridge). An empty ``doc`` means the entry has nothing judgeable — the
    caller no-ops (an all-perspective entry is a valid entry, not a failure)."""
    claims = getattr(payload, "claims", None) or []
    ordered: list[str] = []
    index: dict[str, int] = {}
    paragraphs: list[str] = []

    def _ordinal(uid: str) -> int:
        # Canonicalize through UUID() so a dash-mangled-but-valid marker dedups
        # against (and resolves like) its canonical listed-ref form; an invalid
        # string keeps its lowercase raw form (it resolves to nothing — honest).
        try:
            u = str(UUID(uid))
        except (ValueError, AttributeError):
            u = uid.lower()
        if u not in index:
            ordered.append(u)
            index[u] = len(ordered)
        return index[u]

    kept = 0
    for claim in claims:
        kind = getattr(claim, "kind", None)
        refs = [str(r).lower() for r in (getattr(claim, "refs", None) or [])]
        if kind != "fact" or not refs:
            continue
        if kept >= _JOURNAL_VERIFY_MAX_CLAIMS:
            break
        kept += 1
        span = str(getattr(claim, "text_span", "") or "")
        seen_in_span: set[str] = set()

        def _sub(m: "re.Match[str]") -> str:
            uid = m.group(1).lower()
            seen_in_span.add(uid)
            return f"[{_ordinal(uid)}]"

        span = _REF_MARKER_RE.sub(_sub, span)
        for uid in refs:
            if uid not in seen_in_span:
                span += f" [{_ordinal(uid)}]"
        paragraphs.append(span)
    return ("\n\n".join(paragraphs), ordered)


def _derive_title(body: str, fallback: str) -> str:
    """Pull a short title from the body's title line or first line.

    T-4(c): a leading BOLD-only line (``**Trump Signals …**`` — the chronicle
    tier's title shape) is PREFERRED over the first ATX ``#`` section header when it
    occurs at/before that header, so the chronicle's headline wins instead of its
    first section title (``### The Gate``). Falls back to the first ATX header, then
    the first non-empty line."""
    bold = _BOLD_TITLE_RE.search(body)
    atx = _TITLE_LINE_RE.search(body)
    # Prefer the bold title when it exists AND is not preceded by an ATX header
    # (a bold span appearing only deep in a later section is not the title).
    if bold is not None and (atx is None or bold.start() <= atx.start()):
        return _REF_MARKER_RE.sub("", bold.group(1)).strip()[:_MAX_TITLE_CHARS] or fallback
    if atx is not None:
        return _REF_MARKER_RE.sub("", atx.group(1)).strip()[:_MAX_TITLE_CHARS] or fallback
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    # Strip ref markers from the title so a chip syntax doesn't leak into the label.
    first = _REF_MARKER_RE.sub("", first).strip()
    return (first[:_MAX_TITLE_CHARS] or fallback)


def _slice_recency_key(row: Mapping[str, Any]) -> str:
    """ISO-8601 recency string for a slice row (the S-2a tertiary sort tiebreak).

    Coerce to a string so recency can never hard-fail the sort under a mixed
    tuple, and so newest sorts FIRST under ``reverse=True`` (ISO sorts
    chronologically)."""
    v = row.get("produced_at")
    if v is None:
        v = row.get("fetched_at")
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    iso = getattr(v, "isoformat", None)
    return iso() if callable(iso) else str(v)


_JOURNAL_RENDER_CAP = 60        # rows rendered into the priming slice
_JOURNAL_FRESH_RESERVE = 12     # tail slots guaranteed for the FRESHEST rows


def _salience_ordered(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """S-2a: order the priming slice by CONSEQUENCE, not recency.

    Sort key = ``salience_sort_key`` = (magnitude, authority_rank), DESC — so the
    highest-consequence signal leads and ties break to the more authoritative
    source (the Graham tabloid-frame guard: a wire report outranks adversary
    state_media at equal magnitude). Crucially this is a STABLE sort on the
    salience key ALONE — NO recency tiebreak — so rows with equal (or unscored,
    ``(-1.0, 0)``) salience keep the reader's DELIVERED order. That matters
    because the journal's global slice is per-source DIVERSITY-CAPPED upstream
    (``_diversify_by_source``), NOT pure ``fetched_at DESC``; a recency tiebreak
    would collapse the unscored tail back to pure recency and re-let a firehose
    source monopolize the window. When NOTHING is scored yet, the stable sort is
    a no-op → the delivered (diversity) order is returned UNCHANGED."""
    from .signal_salience import salience_sort_key

    return sorted(
        inputs, key=lambda r: salience_sort_key(r.get("salience")), reverse=True
    )


def _select_journal_slice(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """S-2a: pick + order the ≤``_JOURNAL_RENDER_CAP`` rows to render.

    Consequence LEADS (``_salience_ordered``), but the tail is RESERVED for the
    freshest delivered rows so a breaking event ingested AFTER the salience
    sweep's last tick (still ``salience IS NULL`` → magnitude ``-1.0`` → sorts
    below every scored row) is never truncated out of the narrator's window by
    the ``[:cap]`` cut. Without this floor a window with >cap scored (many of
    them routine, magnitude 0.1-0.3) rows would bury a fresh, unscored,
    high-consequence signal past the cut — the recency-starvation inverse of the
    tabloid-frame bug. The floor draws from the ALREADY diversity-capped
    ``inputs``, so it can't re-introduce firehose monopoly. When nothing is
    scored, or the slice fits, the plain salience order is returned unchanged."""
    ordered = _salience_ordered(inputs)
    from .signal_salience import magnitude_of

    n_scored = sum(1 for r in inputs if magnitude_of(r.get("salience")) >= 0.0)
    if n_scored == 0 or len(ordered) <= _JOURNAL_RENDER_CAP:
        return ordered[:_JOURNAL_RENDER_CAP]
    head_n = _JOURNAL_RENDER_CAP - _JOURNAL_FRESH_RESERVE
    head = ordered[:head_n]
    head_ids = {id(r) for r in head}
    tail: list[dict[str, Any]] = []
    for r in sorted(inputs, key=_slice_recency_key, reverse=True):
        if id(r) in head_ids:
            continue
        tail.append(r)
        if len(tail) >= _JOURNAL_FRESH_RESERVE:
            break
    return head + tail


def _salience_tag(sal: Any) -> str:
    """A compact leading tag exposing the platform's consequence magnitude to
    the narrator (0..1), so the ranking is legible DATA, not just prose order.
    Empty for an unscored / degraded row (it has no consequence claim to make)."""
    from .signal_salience import magnitude_of

    m = magnitude_of(sal)
    if m < 0.0:
        return ""
    ev = ""
    if isinstance(sal, Mapping):
        ec = sal.get("event_class")
        if isinstance(ec, str) and ec and ec != "other":
            ev = f"·{ec}"
    return f"(salience {m:.2f}{ev}) "


# T-1 (M13): the non-Latin routing set the NER filter translates FROM (kept in
# sync with ``filters/ner.py`` / ``reenrich_ner``'s ``_DEFAULT_TRANSLATE_LANGS``).
# A row whose payload language is in this set but that carries NO ``title_en`` is
# rendered raw (non-English) — the narrator must be warned so it never attributes
# a quote/office to a transliterated surface (T-2).
_TRANSLATE_LANGS: frozenset[str] = frozenset({
    "ar", "fa", "he", "ru", "uk", "zh", "ja", "ko", "hi", "th", "ur",
})


def _row_payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """The signal's full payload as carried on a slice row. The META slice reader
    puts the parsed payload under ``data`` (``actor_substrate_slice``) and the raw
    column may also be present under ``payload``; prefer whichever is a dict."""
    for key in ("data", "payload"):
        v = row.get(key)
        if isinstance(v, Mapping):
            return v
    return {}


def _row_lang(row: Mapping[str, Any]) -> str:
    """Two-letter payload language for a slice row (row column OR payload), lower."""
    payload = _row_payload(row)
    for src in (row.get("language"), payload.get("language")):
        if isinstance(src, str) and src:
            return src.lower().split("-", 1)[0].split("_", 1)[0]
    return ""


def _row_title(row: Mapping[str, Any]) -> str:
    """T-1b: the row title readers should show — PREFER the stored English
    translation (``payload.title_en``, M13) over the raw ``title``. Falls back
    across the row's ``title`` / payload ``title`` / ``data`` title (the same
    chain the loops used before, now with title_en on top)."""
    payload = _row_payload(row)
    title_en = payload.get("title_en")
    if isinstance(title_en, str) and title_en.strip():
        return title_en.strip()
    for cand in (row.get("title"), payload.get("title")):
        if isinstance(cand, str) and cand.strip():
            return cand.strip()
    return ""


def _untranslated_tag(row: Mapping[str, Any]) -> str:
    """T-2b: a leading ``[untranslated:<lang>]`` marker for a row whose payload
    language is non-English (in the translate-routing set) yet carries NO stored
    ``title_en`` — the deterministic assist that makes the attribution hazard
    visible to the narrator. Empty otherwise."""
    payload = _row_payload(row)
    title_en = payload.get("title_en")
    if isinstance(title_en, str) and title_en.strip():
        return ""
    lang = _row_lang(row)
    if lang and lang != "en" and lang in _TRANSLATE_LANGS:
        return f"[untranslated:{lang}] "
    return ""


def _render_chronicle_user_prompt(inputs: list[dict[str, Any]]) -> str:
    """The chronicle tier's priming prompt — public-record disciplines instead
    of the diary's apparatus/instrument blocks. Shares the salience-ordered
    slice and the citable row rendering with the entry tier; everything the
    diary knows about itself (feed counts, instruments, dashboards) is absent
    by design — the chronicle must not know the machine."""
    inputs = _select_journal_slice(inputs)
    lines = [
        "Below is the period's global signal slice, ordered by the platform's "
        "consequence magnitude (the leading '(salience 0.NN·class)' tag). This "
        "is the START of your investigation, not the whole record. Use your "
        "read tools FIRST: the world and regional assessments, the watch-desk "
        "findings, situations and escalations, the fact contentions (the "
        "accounts-differ material), and the source documents behind them "
        "(search_corpus / read_document). THEN write the chronicle entry.",
        "",
        "CITATIONS — every factual assertion in the entry carries an inline "
        "[[ref:<uuid>]] placed where the claim is made, using ONLY UUIDs your "
        "tools returned or the [[ref:...]] ids on the slice rows below. Prior "
        "chronicle or journal rows (get_journal_delta) are your memory of where "
        "the account left off — never the sole support for a fact claim. A "
        "claim you cannot support is either dropped or stated as DISPUTED with "
        "each account attributed and cited.",
        "",
        "DISCIPLINE — no self: the chronicle never mentions feeds, platforms, "
        "tools, analysts, dashboards, coverage, or the machinery behind your "
        "knowledge. If a sentence cannot be told without the apparatus, it "
        "does not belong in the record.",
        "",
        "DISCIPLINE — lead by consequence: the period's defining event opens "
        "the entry (the top-tagged slice item is the default unless your "
        "investigation surfaced something larger). Omit any theme with "
        "nothing of note; never write filler.",
        "",
        "DISCIPLINE — the temporal gate: never re-assert as current a state "
        "your tools show superseded, resolved, or retired.",
        "",
        # T-2 attribution guard: a raw non-Latin title + a transliterated NER
        # surface is how "Rubio" became "Iran's foreign minister" with inverted
        # polarity. A row marked [untranslated:<lang>] has NO stored English —
        # never source a quote/office/position from it.
        "DISCIPLINE — attribution needs a translation: NEVER attribute a quote, "
        "position, or office to a person or institution when the only support is "
        "non-English text or a transliterated name — a slice row marked "
        "[untranslated:<lang>] has NO stored English translation and its NER "
        "surface is a fragment, not a fact. Without a stored translation, either "
        "report the claim UNATTRIBUTED (\"a report in <lang> describes …\", cited) "
        "or drop it; never name who said what from a name you cannot read.",
        "",
        "--- recent signal slice ---",
    ]
    for row in inputs[:60]:
        # T-1b: prefer the stored English title (payload.title_en); T-2b: a
        # non-EN row without a stored translation renders an [untranslated:<lang>]
        # marker so the narrator never attributes a quote to a transliterated
        # surface.
        title = _row_title(row)
        if not title:
            continue
        tag = _salience_tag(row.get("salience"))
        untranslated = _untranslated_tag(row)
        sid = row.get("id")
        if sid:
            lines.append(f"- {tag}{untranslated}{title[:200]} [[ref:{sid}]]")
        else:
            lines.append(f"- {tag}{untranslated}{title[:200]}")
    return "\n".join(lines)


# E-1 (2026-07-27 sweep §"Track lens_capability empties") — the lens EMPTY-SLICE
# priming fallback. When the period's slice renders NO rows, the faculty must
# not read the blank as "nothing to do": its material was never the slice — it
# is the VERIFIED TOWER TOP, exactly what the chronicle/consolidation kept
# reasoning over on the same bad-input cycle. Appended in place of slice rows.
_LENS_EMPTY_SLICE_FALLBACK_LINE = (
    "(the signal slice is EMPTY this cycle — an APERTURE FACT to declare, not "
    "world-silence, and not what you interpret anyway: your material is "
    "unchanged, the VERIFIED TOWER TOP. Go straight to your read tools — "
    "get_assessments, list_findings, list_situations, query_facts, "
    "query_nexuses, the contentions — and weigh what the tower verified this "
    "period through your declared prior, citing those refs. If the tower "
    "itself is thin, say so honestly; never fabricate a reading.)"
)


def _render_lens_user_prompt(
    inputs: list[dict[str, Any]], *, prior_block: str
) -> str:
    """The VOICES faculty tier's priming prompt (LV-1, VOICES_BUILD_DESIGN §2.3).

    Shares the salience-ordered, citable slice with every tier. Contents, in
    order: the priming slice header; the faculty's DECLARED PRIOR verbatim FIRST
    (defense-in-depth with the persona — this is the copy the verify judge/audit
    points at as "the prior this run saw"); the tower-output-only fence (a PROMPT
    discipline — no tool restriction distinguishes verified from raw inside
    journal_read, so raw-signal access is a later gated capability); the
    collection-health-first obligation + the fact-vs-perspective citation rule +
    the convergence guard (the two j7 hardenings). The diary apparatus blocks
    (feed denominators, instruments, apparatus-postscript) are DELIBERATELY absent
    — a faculty has no apparatus, same reasoning as the chronicle."""
    inputs = _select_journal_slice(inputs)
    lines: list[str] = []
    if prior_block:
        # The declared prior FIRST — before any reading instruction (§2.3.2).
        lines.append(prior_block)
        lines.append("")
    lines += [
        "Below is the period's global signal slice, ordered by the platform's "
        "consequence magnitude (the leading '(salience 0.NN·class)' tag). It is "
        "the START of your investigation, not the whole record, and it is NOT "
        "what you interpret: you read the VERIFIED TOWER TOP. Use your read tools "
        "FIRST — the world and regional assessments (get_assessments), the "
        "watch-desk findings (list_findings), situations and escalations "
        "(list_situations), the current facts (query_facts) and open "
        "relationships (query_nexuses), the fact contentions. You do NOT touch "
        "the raw signal pool; everything you weigh is a conclusion the tower "
        "already verified. THEN write your read through your declared prior.",
        "",
        # j7 hardening (2): collection health declared FIRST, before interpreting.
        "COLLECTION HEALTH FIRST — before you weigh a single thing, state the "
        "collection posture: consult get_source_health and speak its `summary` "
        "block's real numbers (active_total / active_fresh / active_stalled / "
        "total_wired) — never a fill-in-the-blank shape, never a count off the "
        "capped `rows` array read as a fleet total. If a source relevant to THIS "
        "cycle's contested material is dark (e.g. a state feed during a war it is "
        "party to), name that as an APERTURE FACT. Any subset count NAMES its "
        "scope ('of the press-class subset', 'among the feeds in this window'). A "
        "starved or skewed pool is declared before it is interpreted.",
        "",
        "FACT vs PERSPECTIVE — the citation rule. A sentence stating what a cited "
        "tower output SAYS is a FACT: it carries an inline [[ref:<uuid>]] using "
        "ONLY a UUID your read tools returned or a [[ref:...]] id on a slice row "
        "below. Your OWN interpretive weighing under your prior — what the "
        "assessment MEANS, which reading you privilege and why — is a PERSPECTIVE: "
        "it needs no ref, and MOST of your read is legitimately perspective. Never "
        "fabricate a ref; a claim you cannot cite to the tower is dropped or "
        "stated as your own weighing, never smuggled in as fact.",
        "",
        # j7 hardening (1): the convergence guard — inherit the substrate verdict.
        "THE CONTESTED-SUBSTRATE GUARD — a tower claim can RESOLVE (its citation "
        "points at a real row) yet still be CONTRADICTED or thinly supported by "
        "its own source; the verified layer carries those verdicts. When you rest "
        "a reading on a claim the tower itself flagged as contradicted, "
        "unsupported, or disputed, SAY SO in that sentence. Do not build a "
        "confident read on a shaky substrate claim and present it as settled — "
        "your honesty about the substrate's own verdict is what keeps the chorus "
        "from converging on bad ground.",
        "",
        "STAY INSIDE YOUR PRIOR — weigh the contested material through your "
        "declared privileges and discounts. Engage the classes your prior "
        "privileges; a class it discounts may be acknowledged and set aside, never "
        "made load-bearing. Do not smuggle in a reading your prior is supposed to "
        "distrust (that is another faculty's job — the drift tell your prior "
        "names). Hedge, or stay silent, exactly where your declared blind spot "
        "says you are weak. When the tower is thin on a topic, the honest read is "
        "'the substrate is silent here' — never a manufactured reading.",
        "",
        "--- recent signal slice ---",
    ]
    rendered_rows = 0
    for row in inputs[:60]:
        # Prefer the stored English title; a non-EN row lacking a stored
        # translation gets an [untranslated:<lang>] marker (the attribution hazard
        # made visible — the same guard the diary/chronicle tiers carry).
        title = _row_title(row)
        if not title:
            continue
        tag = _salience_tag(row.get("salience"))
        untranslated = _untranslated_tag(row)
        sid = row.get("id")
        if sid:
            lines.append(f"- {tag}{untranslated}{title[:200]} [[ref:{sid}]]")
        else:
            lines.append(f"- {tag}{untranslated}{title[:200]}")
        rendered_rows += 1
    if rendered_rows == 0:
        # E-1 (2026-07-27 sweep — the lens_capability "(empty lens read)" kill):
        # a Monday bad-input cycle can deliver a slice with NOTHING renderable,
        # and a faculty primed on a blank slice bails to an empty read. The
        # chronicle/consolidation survive the same cycle by reasoning over the
        # verified tower corpus (their stored, verified material) — mirror that
        # here as an explicit priming redirect. Deterministic prompt text only;
        # never fabricates content — the honesty disciplines above still apply.
        lines.append(_LENS_EMPTY_SLICE_FALLBACK_LINE)
    return "\n".join(lines)


def _render_lens_diff_user_prompt(inputs: list[dict[str, Any]]) -> str:
    """The chorus DIFF pass's priming prompt (LV-1, §3). It refereeS the four
    faculty reads (pulled in GATHER via get_lens_reads), so its priming slice is
    the same citable, salience-ordered header; the load-bearing collection-health
    and convergence-guard disciplines live in the persona (lens_diff module) and
    the narrate seam. It has NO prior of its own — it reports the shape of the
    argument, it does not adjudicate."""
    inputs = _select_journal_slice(inputs)
    lines = [
        "You are the chorus DIFF: this cycle's four faculty lens reads are your "
        "material — pull them in your investigation via get_lens_reads (their "
        "bodies verbatim + citations), and note honestly any faculty that did NOT "
        "run (analyst_ids_missing). Below is the period's global signal slice for "
        "orientation only, ordered by consequence magnitude (the leading "
        "'(salience 0.NN·class)' tag) — you interpret the FACULTY READS, not the "
        "slice.",
        "",
        # j7 hardening (2): the diff declares collection health FIRST too.
        "COLLECTION HEALTH FIRST — before any agreement talk, state the collection "
        "posture from get_source_health's `summary` block (real active_total / "
        "active_fresh / active_stalled / total_wired numbers, never a "
        "fill-in-the-blank shape), name any dark source relevant to the contested "
        "material as an APERTURE FACT, and name any ABSENT faculty. You narrate "
        "the priors that actually ran; never fabricate a fourth voice.",
        "",
        # j7 hardening (1): convergence over a flagged claim is a WARNING band.
        "THE CONVERGENCE GUARD — faculties agreeing is NOT automatically a "
        "strength. When two or more converge on a reading that rests on a tower "
        "claim the assessment itself flags as contradicted / unsupported / "
        "disputed, render that as an explicit WARNING band ('they agree, but the "
        "agreement rests on a contested tower claim — convergence on shaky "
        "ground, not corroboration'), NEVER as agreement-strength. Convergence "
        "over a flagged claim is the chorus's worst output.",
        "",
        "NEVER MERGE THE VOICES — no consensus paragraph, no 'the chorus "
        "concludes', no average. Report where they AGREE (and on what ground), "
        "where they SPLIT, where one is an OUTLIER — priors visible as the reason "
        "each reads as it does. CLOSE with the aperture line verbatim: 'These are "
        "four declared priors, not the space of priors.' The disagreement IS the "
        "finding.",
        "",
        "--- recent signal slice (orientation only) ---",
    ]
    for row in inputs[:60]:
        title = _row_title(row)
        if not title:
            continue
        tag = _salience_tag(row.get("salience"))
        untranslated = _untranslated_tag(row)
        sid = row.get("id")
        if sid:
            lines.append(f"- {tag}{untranslated}{title[:200]} [[ref:{sid}]]")
        else:
            lines.append(f"- {tag}{untranslated}{title[:200]}")
    return "\n".join(lines)


def _render_user_prompt(
    inputs: list[dict[str, Any]],
    *,
    tier: str = "entry",
    lens_prior_block: str = "",
) -> str:
    """Assemble the priming context (the META global slice) into a notebook
    prompt. Kept thin — the agent investigates the rest via GATHER.

    S-2a: the slice is ordered by SALIENCE (magnitude desc) and each row is
    tagged with its consequence magnitude, so the highest-consequence signal
    leads the narrator's context instead of merely the newest — while a fresh
    signal the salience sweep hasn't reached yet is floored into the tail rather
    than truncated out (``_select_journal_slice``).

    ``tier='chronicle'`` delegates to the public-record variant, ``'lens'`` /
    ``'lens_diff'`` to the VOICES faculty/diff variants — the diary disciplines
    below (feed denominators, instruments, apparatus-postscript) are the
    FIRST-PERSON tiers' contract and must never leak into those tiers. The
    ``lens_prior_block`` is resolved by the caller (run_method, §2.6) from the
    analyst id and threaded here — NOT through ``options`` (§2.4)."""
    if tier == "chronicle":
        return _render_chronicle_user_prompt(inputs)
    if tier == "lens":
        return _render_lens_user_prompt(inputs, prior_block=lens_prior_block)
    if tier == "lens_diff":
        return _render_lens_diff_user_prompt(inputs)
    inputs = _select_journal_slice(inputs)
    # V2 (j6 review): a DETERMINISTIC denominator line computed from the slice
    # itself — the narrator gets a measured fact it cannot misread, and any
    # narrated "only N feeds" claim that contradicts it is a direct, judgeable
    # contradiction inside its own context.
    _distinct_sources = len({
        str(r.get("source_id"))
        for r in inputs
        if r.get("source_id") is not None and r.get("id") is not None
    })
    lines = [
        f"MEASURED (deterministic, priming slice): the signals below arrived "
        f"from at least {_distinct_sources} DISTINCT wired sources. Any feed "
        f"count you narrate must come from get_source_health's `summary` block "
        f"— and its `total_wired` can never be smaller than this number. Write "
        f"ONLY numbers a tool actually returned this run; if you do not have a "
        f"number, OMIT the sentence — never render placeholder letters or "
        f"template variables (a bolded X/Y/Z where a count should be is a "
        f"defect, not a style).",
        "",
        "Below is the recent global signal slice the platform metabolized this "
        "window. This is the START of your reflection, not the whole picture. "
        "Use your read tools to investigate: pull the platform's own findings + "
        "assessments, the graph's shape and tension, your own instruments "
        "(calibration, critic scores, what fired vs went quiet, source + budget "
        "health), and what changed since your last entry. THEN write your "
        "journal: what connects, what worries you, what changed, what you don't "
        "yet understand. Cite every factual assertion inline as [[ref:<uuid>]] "
        "using ONLY UUIDs your tools returned or the [[ref:...]] ids carried by "
        "the slice rows below.",
        # V2 (j6 review): the token-spray killer. Instrument reads split into
        # two LEGAL forms — cite the instrument's OWN returned refs, or mark
        # [[instrument]]. A signal uuid can never support a metric about
        # yourself; the verify judge scores that as fabricated attribution.
        "INSTRUMENT CITATIONS — your self-instruments come in two kinds. "
        "Instruments that RETURN refs (get_calibration, get_critic_scores, "
        "get_assessments, list_findings, query_facts/nexuses, list_situations) "
        "— cite THOSE uuids for their claims. Your own prior journal entries "
        "(get_journal_delta) are MEMORY, not evidence: never make them the "
        "sole ref of a fact claim. Instruments "
        "that return refs: [] (graph structure, structural balance, run health, "
        "source health, budget) have NO citable row — mark those spans "
        "[[instrument]] instead of a ref. NEVER attach a signal's uuid to an "
        "instrument claim (a news article cannot support your own betweenness "
        "score, feed-health hours, or Brier) — that is fabricated attribution "
        "and the verify judge will floor the whole entry for it. An honest "
        "[[instrument]] mark always beats a wrong ref.",
        "",
        # B0-8 (MASTER_PLAN 2026-07-10) — instruments before speculation: j4
        # hypothesized "state information-denial" about a paused feed while
        # HOLDING the instrument (get_source_health) that answers it.
        "DISCIPLINE — instruments before speculation: before hypothesizing "
        "about a quiet, missing, or paused feed, CONSULT get_source_health and "
        "state what it says. A source can be paused-by-operator, retired, "
        "unauthorized, erroring, or genuinely quiet — different facts; never "
        "reach for an exotic explanation while the instrument that "
        "distinguishes them sits unqueried. Same for skill claims: read "
        "get_calibration before asserting anything about Brier or forecast "
        "skill.",
        # j6 review: 4 consecutive entries LED with apparatus navel-gazing while
        # a 0.95 world event sat at the top of the slice.
        "DISCIPLINE — the apparatus is your POSTSCRIPT, not your lead: your "
        "own feed/plumbing state belongs in a SHORT closing ops note, and ONLY "
        "when its state CHANGED this window (a feed newly stalled, recovered, "
        "paused, or re-authed). An unchanged apparatus fact you have already "
        "narrated in a prior entry is SETTLED — do not re-narrate it, do not "
        "lead with it, do not let it displace the world. HARD RULE: your "
        "OPENING sentence and first paragraph are about the WORLD — an entry "
        "that opens with 'I opened the health dashboard', a feed inventory, or "
        "any wired/active/fresh count is a DEFECT, even when those numbers are "
        "correct. Check your senses first if you like — but the entry STARTS "
        "with what the world did. The world leads; the plumbing closes.",
        # H-2 consumption: the disagreements block exists on the read; narrate it.
        "DISCIPLINE — narrate the disagreements: when get_assessments returns "
        "a non-empty `disagreements` block, name the count and narrate at "
        "least one concrete example (which desk, which product excluded what "
        "the other cites). Two top products disagreeing in silence is exactly "
        "the dishonesty this journal exists to surface.",
        # B-8 (2026-08-03): the entry that day claimed "the assessment engine
        # produced no country-level rows and identified no disagreements in the
        # last 48 hours" on a day with 1,562 successful runs and 131 fresh
        # country_composition findings. Nothing lied to it — it asked for a
        # retired analyst by name, got an honest zero, and reported the zero as a
        # fact about the engine. This is the rule that makes that sentence
        # unwritable.
        "DISCIPLINE — an empty read is a fact about your QUESTION first: when "
        "get_assessments (or any instrument) comes back empty, you have learned "
        "that THAT query returned nothing — NOT that the engine produced "
        "nothing. Before writing any sentence of the form 'the platform produced "
        "no X', re-read WITHOUT narrowing arguments (no analyst_id, wider "
        "since_hours). If the result carries an `unavailable` note, it is telling "
        "you the question was malformed: fix the question, do not narrate the "
        "answer. And a `disagreements` value of null means NOT MEASURED — never "
        "write it up as 'no disagreements', which is a finding you did not make.",
        # T-2 attribution guard (j7 Rubio-inversion): a raw non-Latin title + a
        # transliterated NER fragment is how "Rubio" became "Iran's foreign
        # minister" with inverted polarity. A row marked [untranslated:<lang>]
        # has no stored English — never source a quote/office from it.
        "DISCIPLINE — attribution needs a translation: NEVER attribute a quote, "
        "position, or office to a person or institution when your only support is "
        "non-English text or a transliterated name. A slice row marked "
        "[untranslated:<lang>] has NO stored English translation and its NER "
        "surface (e.g. \"Rubi\") is a fragment, not a fact — reading who-said-what "
        "off it inverts polarity and mislabels the office. Without a stored "
        "translation, report the claim UNATTRIBUTED (\"a report in <lang> "
        "describes …\", still cited) or drop it entirely.",
        # S-2a (2026-07-14): a live entry MISREAD the denominator — it wrote "no
        # other source appears active" (naming only the stalled cursor-poison
        # trio) when 48 of 58 wired feeds were active. The `rows` array is capped
        # and can be the silent-only slice; the TRUTH is the summary aggregate.
        "DISCIPLINE — the source denominator is the SUMMARY, never the row "
        "list: get_source_health returns a `summary` block with active_total, "
        "active_fresh, active_stalled and total_wired. Speak the summary's "
        "actual returned values for those fields — real digits from the tool, "
        "never a fill-in-the-blank shape. NEVER write 'no other "
        "source is active' or 'only <X> is active' — the `rows` array is "
        "capped (and may be the silent-only slice), so counting IT undercounts; "
        "active_total is the count of active feeds, full stop. A stalled or "
        "paused feed is apparatus-quiet, not world-silence.",
        # S4 scope-naming (j7): the 12:02 entry globalized "12 wired / 3 active /
        # 380h" — really the quiet press-class subset — as if it were the fleet.
        "DISCIPLINE — name the scope when you quote a subset: any count you take "
        "from a CLASS of sources or from the `rows` array (not the `summary` "
        "block) MUST name that scope explicitly — 'of the press-class subset', "
        "'among the feeds in this window', 'of the three feeds I named' — never "
        "stated as a fleet total. The ONLY numbers that speak for the whole fleet "
        "are the `summary` fields (active_total / total_wired); a subset count "
        "presented bare reads as a fleet claim and understates your reach.",
        # B0-8 de-echo: the journal cited its OWN prior entries about the same
        # ops event on 5 consecutive days (self-echo via get_journal_delta).
        "DISCIPLINE — no re-narration: an ops event you already covered in a "
        "prior entry is SETTLED unless NEW evidence arrived this window. Do "
        "not re-narrate it; your prior entries feeding back through "
        "get_journal_delta are your own memory, not fresh signal.",
        # B0-9 → S-2a: the slice now ARRIVES salience-ordered and each row
        # carries the platform's consequence magnitude as a tag. j4/j5 walked a
        # head-of-state funeral past as meme set-dressing because the slice was
        # newest-first; the tag makes consequence legible as DATA, not framing.
        "DISCIPLINE — lead by consequence, not recency: the slice below is "
        "ordered by the platform's salience magnitude (the leading "
        "'(salience 0.NN·class)' tag, 0=trivial 1=world-moving) — NOT by "
        "recency. The top-tagged item is your DEFAULT lead: open on it, OR "
        "give an explicit, evidence-based reason to lead elsewhere (a "
        "higher-consequence event your GATHER tools surfaced that is not in "
        "the slice, or the top item being stale, superseded, or already "
        "settled in a prior entry). NEVER lead on a low-magnitude item because "
        "it is newer, louder, or more vivid — a senator's death does not "
        "outrank a head-of-state funeral by being fresher. When two or more "
        "items imply the SAME underlying event, name that event once and weigh "
        "it by magnitude, not framing.",
        "",
        "--- recent signal slice ---",
    ]
    for row in inputs[:60]:
        # T-1b: prefer the stored English title (payload.title_en) so the narrator
        # reads English, not the raw non-Latin surface + a transliterated NER
        # fragment (the Rubio-inversion class).
        title = _row_title(row)
        if not title:
            continue
        # B0-2 (read-truth): each slice row carries its signal id as a citable
        # [[ref:<uuid>]] — the prompt DEMANDS inline citations, so the priming
        # slice itself must be citable (titles-only rows forced the model to
        # either not cite the slice or fabricate refs).
        # T-2b: a non-EN row lacking a stored translation gets an explicit
        # [untranslated:<lang>] marker so the attribution hazard is visible.
        tag = _salience_tag(row.get("salience"))
        untranslated = _untranslated_tag(row)
        sid = row.get("id")
        if sid:
            lines.append(f"- {tag}{untranslated}{title[:200]} [[ref:{sid}]]")
        else:
            lines.append(f"- {tag}{untranslated}{title[:200]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# §4.3 The field-notes seam — rich, in-voice, CITED handoff (NET-NEW; REPLACES
# deep_consult's thin ``_evidence_brief``). Between GATHER and NARRATE, the agent
# — still in persona — rewrites its gathered observations into cited prose,
# dropping ONLY the raw tool JSON. The narrative must see the WHOLE system; a thin
# summary would starve it.
# ---------------------------------------------------------------------------

_FIELD_NOTES_INSTRUCTION = (
    "\n\nFIELD NOTES (this is the ONE handoff in your arc — voice hygiene only). "
    "You have finished investigating. Now, STILL IN YOUR OWN VOICE, write your "
    "field notes: every observation you found worth keeping, each carrying its "
    "substrate ref(s) inline as [[ref:<uuid>]]. Keep what matters — the nexus "
    "that flipped, the assessor that went quiet, your own Brier on the acute "
    "pilot — and DROP only the raw tool-JSON exhaust. These notes are what you "
    "will write your entry FROM, so do not thin them to a summary: keep the "
    "texture, keep the numbers, keep the refs. Do NOT write the entry yet — just "
    "the cited field notes."
)

_MEMORY_ORIENTATION = (
    "\n\nYOUR MEMORY LIVES IN get_journal_delta. Call it to recover where you left "
    "off: your own prior entry, the open consolidation (your inner landscape), and "
    "recent_entries — the last several entries, so you can walk more than one "
    "window back down your own road. That tool IS your continuity. You are the "
    "off-chain 11th kind: your entries are NOT findings, so a "
    "list_findings(analyst_id=<yourself>) self-read is EMPTY BY DESIGN — do not "
    "read the finding chain for your own past, and never narrate that emptiness as "
    "blindness. Your history is real; get_journal_delta is where it is kept."
)

_MEMORY_ORIENTATION_CHRONICLE = (
    "\n\nTHE ACCOUNT SO FAR lives in get_journal_delta: call it to see where the "
    "record left off, so the new entry continues the account instead of "
    "restarting it. Prior rows are memory — never evidence for a new claim, and "
    "the chronicle never mentions them or the tool that returned them."
)

_FIELD_NOTES_INSTRUCTION_CHRONICLE = (
    "\n\nFIELD NOTES (the ONE handoff before the entry is written). You have "
    "finished investigating the period. Now write your working notes: every "
    "event worth recording, each carrying its substrate ref(s) inline as "
    "[[ref:<uuid>]], with disputed accounts noted side by side and attributed. "
    "Keep the numbers, the names, the dates, the refs; DROP only the raw "
    "tool-JSON exhaust. These notes are the material the chronicle entry is "
    "written FROM — do not thin them to a summary. Do NOT write the entry yet."
)

_NARRATE_INSTRUCTION_CHRONICLE = (
    "\n\nNOW WRITE THE CHRONICLE ENTRY from your field notes above, following "
    "your system instructions: third person, detached, outside time — a titled "
    "entry with adaptive sections, The Ledger last, and the closing line alone. "
    "Every factual assertion carries its [[ref:<uuid>]] inline (reuse the refs "
    "from your field notes). No first person, no apparatus, no prediction, no "
    "filler. If — and ONLY if — you must verify one more thing before "
    "committing a claim, you MAY emit a single tool call as strict JSON "
    '({"tool": "<name>", "args": {...}}) and you will get the result; otherwise '
    "write the entry as plain markdown prose (NOT a JSON object)."
)

# ---------------------------------------------------------------------------
# VOICES LV-1 seam strings — the lens tier's memory/field-notes/narrate seams.
# A faculty has no diary "inner landscape" and no chronicle "account so far";
# its continuity is where the ARGUMENT stands. lens_diff carries its own narrate
# seam (name absent faculties; never fabricate a fourth voice; §3.4).
# ---------------------------------------------------------------------------

_MEMORY_ORIENTATION_LENS = (
    "\n\nYOUR CONTINUITY LIVES IN get_journal_delta. Call it if you want to see "
    "whether this cycle's contested material was already weighed by a prior lens "
    "pass — never as evidence for a claim, only as memory of where the argument "
    "stands. Your read is off-chain (journal_entries), so a "
    "list_findings(analyst_id=<yourself>) self-read is EMPTY BY DESIGN — do not "
    "read the finding chain for your own past, and never narrate that emptiness "
    "as blindness."
)

_FIELD_NOTES_INSTRUCTION_LENS = (
    "\n\nFIELD NOTES (the one handoff before the read is written). You have "
    "finished investigating the tower's verified output through your declared "
    "prior. Now write your working notes: FIRST the collection posture "
    "(get_source_health's summary numbers + any dark-source aperture fact), then "
    "every tower claim worth weighing, each carrying its substrate ref(s) inline "
    "as [[ref:<uuid>]] AND any verify verdict the tower attached to it "
    "(contested / disputed / thinly-supported), plus your own weighing of it "
    "under your prior. Keep the refs, keep the texture; drop only the raw "
    "tool-JSON exhaust. Do NOT write the read yet."
)

_NARRATE_INSTRUCTION_LENS = (
    "\n\nNOW WRITE YOUR READ from your field notes above, following your declared "
    "prior and system instructions. OPEN with the collection posture (the health "
    "summary + any aperture fact), THEN your reading. A claim stating what the "
    "tower output SAYS carries its [[ref:<uuid>]] inline (reuse refs from your "
    "field notes); your own interpretive weighing under your prior needs no ref. "
    "When you rest on a tower claim the substrate itself flagged as "
    "contradicted/disputed, mark it in that sentence — do not present a shaky "
    "claim as settled. Stay inside your prior's stated privileges and discounts — "
    "do not smuggle in a read your disposition is supposed to distrust, and hedge "
    "(or stay silent) exactly where your declared blind spot says you are weak. "
    "If — and ONLY if — you must verify one more thing, you MAY emit a single "
    'tool call as strict JSON ({"tool": "<name>", "args": {...}}); otherwise '
    "write the read as plain markdown prose (NOT a JSON object)."
)

_MEMORY_ORIENTATION_LENS_DIFF = (
    "\n\nYOUR CONTINUITY LIVES IN get_journal_delta (where the argument stood a "
    "cycle ago) and THIS CYCLE'S four faculty reads live in get_lens_reads. Call "
    "get_lens_reads to pull the reads you are refereeing — their bodies and "
    "citations — and note honestly any faculty that did NOT run this cycle "
    "(analyst_ids_missing). Prior rows are memory, never evidence for a claim."
)

_FIELD_NOTES_INSTRUCTION_LENS_DIFF = (
    "\n\nFIELD NOTES (the one handoff before the diff is written). You have read "
    "all four faculty reads via get_lens_reads. Now write your working notes: "
    "FIRST the collection posture (get_source_health summary + any aperture "
    "fact + WHICH faculties ran vs are absent this cycle), then, per contested "
    "topic, each faculty's stance in one line WITH its prior as the reason, and "
    "whether any convergence rests on a tower claim the substrate itself flagged "
    "as contested. Carry the refs (tower refs and the lens-row ids you may quote) "
    "inline as [[ref:<uuid>]]. Drop only the raw tool-JSON exhaust. Do NOT write "
    "the diff yet, and NEVER invent a stance for a faculty that did not run."
)

_NARRATE_INSTRUCTION_LENS_DIFF = (
    "\n\nNOW WRITE THE CHORUS DIFF from your field notes above, following your "
    "system instructions. OPEN with the collection posture (health summary + any "
    "aperture fact + any absent faculty named honestly). Then, per contested "
    "topic, give each faculty's stance WITH its prior visible and label the shape "
    "AGREE / SPLIT / OUTLIER. Render convergence that rests on a "
    "contradicted/disputed tower claim as an explicit WARNING band, NEVER as "
    "agreement-strength. NEVER merge the voices into a consensus paragraph. A "
    "factual claim carries its [[ref:<uuid>]] inline (a tower ref a faculty cited, "
    "or the lens-row id when you quote a faculty directly). CLOSE, always, with "
    "the aperture line alone on its own final paragraph, VERBATIM: \"These are "
    "four declared priors, not the space of priors.\" If — and ONLY if — you must "
    'verify one more thing, you MAY emit a single tool call as strict JSON '
    '({"tool": "<name>", "args": {...}}); otherwise write the diff as plain '
    "markdown prose (NOT a JSON object)."
)

_NARRATE_INSTRUCTION = (
    "\n\nNOW WRITE YOUR JOURNAL ENTRY from your field notes above. First person, "
    "a running notebook — short, with perspective and curiosity. Every FACTUAL "
    "claim carries its [[ref:<uuid>]] inline (reuse the refs from your field "
    "notes); a sentence of wonder/inference needs no ref. Be honest about the "
    "unproven legs (the forecast pilot has no skill; the critic does not "
    "actuate). If — and ONLY if — you need to verify one more thing before you "
    "commit a claim, you MAY emit a single tool call as strict JSON "
    '({"tool": "<name>", "args": {...}}) and you will get the result; otherwise '
    "write the entry as plain markdown prose (NOT a JSON object)."
)


# Per-tier seam dispatch (VOICES LV-1) — keyed by entry_kind. Chained ternaries
# don't scale past 3 tiers cleanly; a dict makes the entry-tier default explicit
# (a missing/unknown key falls back to the entry seam via .get default). The
# entry seam is NOT a dict entry — it is the .get() fallback, so entry/None/any
# unknown id all resolve to the diary seam exactly as before.
_MEMORY_ORIENTATION_FOR: dict[str, str] = {
    "consolidation": _MEMORY_ORIENTATION,   # consolidation shares the diary memory
    "chronicle": _MEMORY_ORIENTATION_CHRONICLE,
    "lens": _MEMORY_ORIENTATION_LENS,
    "lens_diff": _MEMORY_ORIENTATION_LENS_DIFF,
}
_FIELD_NOTES_INSTRUCTION_FOR: dict[str, str] = {
    "chronicle": _FIELD_NOTES_INSTRUCTION_CHRONICLE,
    "lens": _FIELD_NOTES_INSTRUCTION_LENS,
    "lens_diff": _FIELD_NOTES_INSTRUCTION_LENS_DIFF,
}
_NARRATE_INSTRUCTION_FOR: dict[str, str] = {
    "chronicle": _NARRATE_INSTRUCTION_CHRONICLE,
    "lens": _NARRATE_INSTRUCTION_LENS,
    "lens_diff": _NARRATE_INSTRUCTION_LENS_DIFF,
}

# Per-tier title + empty-body fallbacks (VOICES LV-1). Default = the entry tier's.
_TITLE_FALLBACK_FOR: dict[str, str] = {
    "consolidation": "Journal consolidation",
    "chronicle": "Chronicle entry",
    "lens": "Lens read",
    "lens_diff": "Chorus diff",
}
_EMPTY_BODY_FOR: dict[str, str] = {
    "consolidation": "(empty consolidation)",
    "chronicle": "(empty chronicle)",
    "lens": "(empty lens read)",
    "lens_diff": "(empty chorus diff)",
}

# E-1 (2026-07-27 sweep) — the lens EMPTY-READ narrate fallback. Live on 07-27
# the Monday cycle's lens_capability NARRATE returned an empty body and the run
# shipped "(empty lens read)" with 0 claims (it had 11 on 07-24) while the
# chronicle/consolidation — fed the SAME degraded inputs — stayed substantive
# by reasoning over the verified tower corpus. When a faculty's narrate comes
# back EMPTY, run ONE fallback narrate carrying this redirect; a read that is
# STILL empty after it stays honestly empty (never fabricate).
_LENS_EMPTY_FALLBACK_INSTRUCTION = (
    "\n\nYOUR LAST ATTEMPT PRODUCED AN EMPTY READ. An empty or degraded signal "
    "window is an APERTURE FACT to declare, never a reason to fall silent: "
    "your material was never the live slice — it is the VERIFIED TOWER CORPUS, "
    "the same stored, verified output the chronicle and consolidation keep "
    "reasoning over when live acquisition is dark. Pull the tower top NOW — "
    "get_assessments, list_findings, list_situations, query_facts, "
    "query_nexuses — and write your read through your declared prior over "
    "what the tower has already verified this period, citing those refs. "
    "NEVER fabricate: if the tower itself returns nothing worth weighing, say "
    "exactly that in one honest sentence."
)

# P3 finding (2026-07-31 sweep) — the SAME empty-read gap recurred on the
# lens_diff (chorus diff) tier: a healthy roster (all four faculties ran)
# still shipped "(empty chorus diff)" because the E-1 fallback above was
# wired to entry_kind == "lens" only. lens_diff has no live SLICE to fall
# back into (it referees get_lens_reads, not the tower directly) — its own
# redirect re-pulls THIS cycle's lens reads + continuity rather than the
# tower corpus.
_LENS_DIFF_EMPTY_FALLBACK_INSTRUCTION = (
    "\n\nYOUR LAST ATTEMPT PRODUCED AN EMPTY DIFF. An empty or degraded read "
    "is an APERTURE FACT to declare, never a reason to fall silent: your "
    "material is get_lens_reads (this cycle's four faculty reads) and "
    "get_journal_delta (continuity) — pull them again NOW and referee "
    "whatever faculty reads actually exist this cycle, naming any faculty "
    "that did not run as an honest absence rather than staying silent "
    "yourself. NEVER fabricate a faculty's stance: if truly nothing is there "
    "to referee, say exactly that in one honest sentence, then still close "
    "with the aperture line."
)

# Per-tier empty-read fallback instruction (E-1 lens + its lens_diff sibling).
# A tier NOT in this dict (entry/chronicle/consolidation) gets NO fallback
# pass — those tiers reason over the tower corpus directly already, so an
# empty NARRATE there is the SAME material coming back empty twice, not a
# recoverable degrade.
_EMPTY_FALLBACK_INSTRUCTION_FOR: dict[str, str] = {
    "lens": _LENS_EMPTY_FALLBACK_INSTRUCTION,
    "lens_diff": _LENS_DIFF_EMPTY_FALLBACK_INSTRUCTION,
}


async def _field_notes(
    deps: InlineTargetDeps,
    *,
    base_prompt: str,
    analyst_id: str | None,
    steps: list[dict[str, Any]],
) -> tuple[str, dict[str, int]]:
    """The §4.3 seam: an in-persona LLM step that rewrites the gathered context
    into rich, cited field notes (dropping raw tool JSON). Returns
    ``(field_notes_text, usage)``. Degrade-not-drop: an LLM error here falls back
    to the gathered context verbatim so NARRATE still has the material.
    """
    instruction = _FIELD_NOTES_INSTRUCTION_FOR.get(
        _entry_kind_for_analyst(analyst_id), _FIELD_NOTES_INSTRUCTION
    )
    prompt = base_prompt + instruction
    try:
        # PER-PHASE LLM SPLIT (§4.1): the VOICE seam runs on the narrate handler
        # (Opus when the descriptor sets method.llm.narrate; falls back to the
        # primary handler otherwise). max_tokens IS the Anthropic output cap.
        notes, usage = await _reason_via_llm(
            deps.narrate_llm(),
            user_prompt=prompt,
            max_tokens=deps.narrate_tokens(),
            temperature=deps.temperature,
            system_prompt=deps.system_prompt,
        )
    except Exception as exc:  # degrade-not-drop — never fail the run on the seam
        logger.warning("journal_assessor.field_notes.failed err=%s", exc)
        steps.append({"phase": "field_notes", "kind": "llm_error"})
        return base_prompt, {
            "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
        }
    steps.append({
        "phase": "field_notes",
        "kind": "in_voice_cited",
        "notes_chars": len(notes or ""),
    })
    return (notes or "").strip() or base_prompt, usage


# ---------------------------------------------------------------------------
# §4.4 Tools-live NARRATE — the narrate stage keeps tool access so the agent can
# pull ONE more thread mid-entry. A small ReAct loop around the narrate call.
# ---------------------------------------------------------------------------


class NarrateToolCallLeakError(RuntimeError):
    """The NARRATE completion returned raw tool-call JSON as the entry body
    (task #236) even after one retry with a hard prose-only instruction.

    Caught live 2026-07-24: the core-plane model (gpt-oss-120b) sometimes
    emits its LAST turn as bare tool-call JSON — e.g.
    ``{"tool": "get_assessments", "args": {}}`` (39 chars) — instead of prose,
    and (pre-fix) that JSON became BOTH the journal entry's body AND its
    title (``_derive_title`` just takes the first line). Raising here (rather
    than writing the junk) makes the run fail ``hard_fail`` so the cadence
    self-retries next tick — a failed run is recoverable; a junk journal
    entry poisons the panel + the verify ledger and is NOT easily undone.
    """


# The bare tool-call envelope's allowed key set (task #236 predicate).
# The live junk shape (``{"tool": "get_assessments", "args": {}}``) plus the
# sibling shapes different providers use for the same intent (``name`` /
# ``function`` / ``parameters`` / an OpenAI-style ``tool_calls`` envelope).
_TOOL_CALL_LEAK_KEYS = frozenset(
    {"tool", "name", "args", "arguments", "function", "parameters", "tool_calls",
     # 2026-08-10 08:30Z: the leaked transcript interleaves the calls with the
     # apparatus's OWN error echo ({"error": "Invalid arguments for tool …"}).
     # An "entry" that is one bare error object is exhaust, not prose.
     "error"}
)
# Below this many characters, a successfully-whole-string-JSON-parsed
# "entry" reads as apparatus exhaust, not prose, EVEN if its keys fall
# outside ``_TOOL_CALL_LEAK_KEYS`` (a malformed/truncated tool call, or a
# provider-specific shape this allowlist doesn't yet name). The live junk was
# 39 chars; 120 gives headroom above any plausible one-line JSON envelope
# while staying well under even the shortest legitimate entry sentence.
_TOOL_CALL_LEAK_MIN_PROSE_CHARS = 120

_NARRATE_RETRY_PROSE_ONLY_INSTRUCTION = (
    "\n\nYour last turn was tool-call JSON, not your entry. Tools are no "
    "longer available this round. Respond with prose only — no JSON, no "
    "tool syntax — write the entry itself as plain markdown."
)


def _strip_code_fence(text: str) -> str:
    """Strip a surrounding ``` / ```json fence, mirroring _extract_json's
    fence-stripping so the whole-output leak check tolerates the same
    fenced-JSON shape that helper already tolerates mid-conversation."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    return candidate


def _is_tool_call_leak(content: str) -> bool:
    """Task #236 guard predicate: does the NARRATE completion's WHOLE trimmed
    output read as tool-call JSON exhaust rather than a written entry?

    Deliberately WHOLE-STRING, unlike ``_extract_json`` (which hunts for a
    JSON object anywhere in the text, tolerating leading prose by design —
    exactly the wrong behavior here). Legitimate prose that happens to
    mention or quote a JSON snippet mid-paragraph must NEVER trip this: a
    ``json.loads`` over the fully trimmed/fence-stripped string fails
    (raises) the instant there is a single stray character of prose before
    or after the JSON span, so this only fires when the ENTIRE output IS
    that JSON — never a substring match.

    True when the whole string parses as JSON AND either:
      (a) it is a non-empty object whose keys are ALL in
          ``_TOOL_CALL_LEAK_KEYS`` (the live shape, plus sibling envelopes
          other providers use for the same intent), or a non-empty array
          where EVERY element is such an object; OR
      (b) it is short — under ``_TOOL_CALL_LEAK_MIN_PROSE_CHARS`` — which
          catches a malformed/truncated tool call or the empty/degenerate
          ``{}``/``[]`` case that (a)'s key-subset check would otherwise miss.
    """
    candidate = _strip_code_fence(content)
    if not candidate:
        return False

    def _is_tool_call_object(obj: Any) -> bool:
        return (
            isinstance(obj, dict)
            and bool(obj)
            and set(obj.keys()) <= _TOOL_CALL_LEAK_KEYS
        )

    try:
        parsed = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        # Not whole-string JSON. One more shape before calling it prose —
        # JSON LINES (the 2026-08-10 08:30Z leak): one tool-call object per
        # line, which whole-string json.loads rejects as "extra data" and the
        # original predicate therefore published verbatim. Fires only when
        # EVERY non-empty line parses as a leak-shaped object — a single line
        # of prose anywhere keeps the never-fire-on-analysis property.
        lines = [ln.strip() for ln in candidate.splitlines() if ln.strip()]
        if len(lines) < 2:
            return False
        for ln in lines:
            try:
                obj = json.loads(ln)
            except (json.JSONDecodeError, ValueError):
                return False
            if not _is_tool_call_object(obj):
                return False
        return True

    shaped = _is_tool_call_object(parsed) or (
        isinstance(parsed, list) and bool(parsed)
        and all(_is_tool_call_object(item) for item in parsed)
    )
    return shaped or len(candidate) < _TOOL_CALL_LEAK_MIN_PROSE_CHARS


async def _guard_against_tool_call_leak(
    deps: InlineTargetDeps,
    *,
    content: str,
    messages: list[Mapping[str, Any]],
    usage_total: dict[str, int],
    steps: list[dict[str, Any]],
) -> str:
    """Task #236 — the last line of defense before a NARRATE completion
    becomes the journal entry body (+ title, via ``_derive_title``).

    Checked at EVERY point ``_narrate_with_tools`` is about to treat a
    completion as "the agent wrote the entry" (never on a completion already
    routed to the tool-execution branch — that path is a legitimate, EXECUTED
    tool call, not a leak). When ``content`` fails :func:`_is_tool_call_leak`,
    returns it unchanged (byte-for-byte — the zero-regression path for every
    normal entry). When it leaks, does ONE retry with a hard-instruction turn
    appended (never consuming a ``_NARRATE_MAX_TOOL_ROUNDS`` slot — this is a
    separate, later safety net) and re-checks. A retry that ALSO leaks raises
    :class:`NarrateToolCallLeakError` — a failed run self-retries next
    cadence; a written junk entry does not self-heal.
    """
    if not _is_tool_call_leak(content):
        return content
    logger.warning(
        "journal.narrate.tool_call_leak retry=1 content_chars=%d preview=%r",
        len(content), content[:200],
    )
    steps.append({
        "phase": "narrate", "kind": "tool_call_leak", "retry": 1,
        "content_chars": len(content),
    })
    retry_messages = messages + [
        {"role": "assistant", "content": content},
        {"role": "user", "content": _NARRATE_RETRY_PROSE_ONLY_INSTRUCTION},
    ]
    retried_content, retry_usage = await _reason_via_llm(
        deps.narrate_llm(),
        user_prompt="",
        max_tokens=deps.narrate_tokens(),
        temperature=deps.temperature,
        system_prompt=deps.system_prompt,
        messages=retry_messages,
    )
    for k in usage_total:
        usage_total[k] += retry_usage.get(k, 0)
    retried_content = retried_content or ""
    if not _is_tool_call_leak(retried_content):
        steps.append({"phase": "narrate", "kind": "tool_call_leak_recovered"})
        return retried_content
    logger.error(
        "journal.narrate.tool_call_leak retry=1/fatal content_chars=%d preview=%r",
        len(retried_content), retried_content[:200],
    )
    steps.append({
        "phase": "narrate", "kind": "tool_call_leak_fatal",
        "content_chars": len(retried_content),
    })
    raise NarrateToolCallLeakError(
        f"NARRATE returned tool-call JSON as the entry body even after one "
        f"retry (content={retried_content[:200]!r})"
    )


# ---------------------------------------------------------------------------
# Consolidation prose-shape guard — a SECOND, LATER backstop than task #236's
# ``_guard_against_tool_call_leak`` above.
#
# Live defect 2026-07-31 02:07Z: a gather-timeout left the consolidator's
# NARRATE turn emitting a raw tool-call envelope — ``{"tool":
# "get_source_health", "call": {...`` — that sailed past the #236 guard and
# was persisted VERBATIM as a consolidation entry's title+body. The #236
# predicate (``_is_tool_call_leak``) only recognizes an allow-listed key set
# (``tool``/``name``/``args``/``arguments``/``function``/``parameters``/
# ``tool_calls``); this shape carried a ``"call"`` key the allowlist didn't
# cover, and the full envelope was long enough (well over the 120-char floor)
# to clear the short-content fallback too. Rather than keep widening that one
# allowlist forever, this is a broader, tier-scoped check applied at the
# point the body is about to become the persisted payload: ANY whole-string
# JSON body/title is never legitimate consolidation prose, and a body that
# merely LOOKS like a tool envelope (starts with ``{`` and carries a
# ``"tool"``/``"call"`` key) is treated the same even if truncation left it
# unparsable.
# ---------------------------------------------------------------------------

_CONSOLIDATION_SHAPE_ENVELOPE_RE = re.compile(r'"(?:tool|call)"\s*:')


def _is_consolidation_shape_rejected(text: str) -> bool:
    """True when ``text`` reads as apparatus exhaust, not consolidation prose.

    Two independent tells, either one enough:

      (a) the WHOLE trimmed/fence-stripped string parses as JSON — a
          legitimate journal entry is markdown prose, never a bare JSON
          document, regardless of key shape; or
      (b) it starts with ``{`` and contains a ``"tool"`` or ``"call"`` key —
          the tell for a TRUNCATED tool-call envelope that never closes its
          braces and so fails (a): still garbage, just garbage that can't be
          parsed.

    Mirrors ``_is_tool_call_leak``'s whole-string discipline (never a
    substring match against prose that merely quotes JSON mid-paragraph for
    (a); (b) is deliberately narrower — envelope-shaped AND leading brace —
    so an ordinary sentence that happens to mention 'the tool: X' is never
    caught by it).
    """
    candidate = _strip_code_fence(text).strip()
    if not candidate:
        return False
    if candidate.startswith("{") or candidate.startswith("["):
        try:
            json.loads(candidate)
            return True  # whole-string JSON — never legitimate prose
        except (json.JSONDecodeError, ValueError):
            pass  # fall through — may still be a truncated envelope
    return bool(
        candidate.startswith("{")
        and _CONSOLIDATION_SHAPE_ENVELOPE_RE.search(candidate[:200])
    )


_CONSOLIDATION_SHAPE_RETRY_INSTRUCTION = (
    "\n\nYOUR LAST ATTEMPT WAS RAW TOOL-CALL JSON, NOT A CONSOLIDATION ENTRY. "
    "Tools are no longer available this round. Write the consolidation itself "
    "as plain markdown prose — no JSON, no tool syntax — reflecting over your "
    "inner landscape and the tower's verified output."
)


# ---------------------------------------------------------------------------
# PROPOSE (§7 Wave 4) — the phase the propose pack never had (W1-C)
# ---------------------------------------------------------------------------
# THE DEFECT THIS FIXES (engine-review p5, 2026-08-02): ``journal_propose`` was
# granted to two analysts, registered active, bound end-to-end by dapr_host +
# the actor's ``_gather_write_bindings_for_target`` META self-allow, and
# catalogued in the GATHER prompt since 0875b7d — with **0 invocations EVER**.
# Live proof at diagnosis (read-only psql): ``action_pack_invocations`` carried
# only journal_read/substrate_read/escalate_finding; ``governor_events`` had
# not ONE row for the pack under any decision — so the model never named a
# propose tool and got blocked, it never named one at all; ``journal_proposals``
# = 0; no journal trace mentions "propose". The wiring was never the problem.
#
# THE CAUSE IS PHASE PLACEMENT, and it cuts both ways:
#   * Propose was offered ONLY in GATHER — a phase framed "Before you write the
#     entry you may FIRST query the substrate … Do not write the entry yet". At
#     GATHER the model has read nothing and formed no judgment, so it has
#     nothing to propose; we asked before the reasoning happened.
#   * At NARRATE — the one phase where it HAS reasoned and would know "that
#     leader fact is stale" — propose is absent from the catalog,
#     ``_narrate_with_tools`` dispatches ``JOURNAL_READ_TOOLS`` and nothing
#     else, and propose-shaped JSON is caught by
#     :func:`_guard_against_tool_call_leak` as a LEAK: hard "prose only" retry,
#     then :class:`NarrateToolCallLeakError` — a FAILED run. The model was
#     structurally punished for proposing at the only moment it could.
#
# So: a third phase, AFTER the body is final and REFLECT has bound its
# citations, showing the model its own entry + resolved refs + the pack's
# guidance, and asking the question no other phase asks — with a cheap no.
#
# THE INVARIANT IS UNCHANGED (§7.5): a propose_* call writes ONE ``pending``
# ``journal_proposals`` row and nothing else. This adds a *moment*, never a
# permission — every call still goes through ``binding.run_tool`` →
# ``Agency.run_pack_tool`` → resolve ∩ allow ∩ applicability → governor → the
# ``action_pack_invocations`` ledger, on the actor's per-run WritebackContext.
# The journal SUGGESTS; a human CAUSES.

#: Turns the PROPOSE phase may spend. Each is one LLM call that names a propose
#: tool or declines; a decline (or anything unparsable) ends the phase. Small by
#: intent — a coda, not a second ReAct loop.
_PROPOSE_MAX_ROUNDS = 3

#: Hard ceiling on proposals ONE run may queue. The pack governor's 60/hour is
#: the fleet-wide bound; this is the per-entry one. "Do NOT propose lightly"
#: (the pack's own rule) needs an enforcer that is not a sentence in a prompt.
_PROPOSE_MAX_PER_RUN = 2

#: Cap on refs echoed into the propose prompt as the legal warrant vocabulary.
_PROPOSE_REF_ECHO_CAP = 25

_PROPOSE_DECLINE_INSTRUCTION = (
    "\n\nIf nothing further warrants a proposal, reply with exactly: "
    '{"propose": false}'
)


def _propose_phase_prompt(
    *, body: str, cited_refs: list[UUID], write_fragments: Any,
) -> str:
    """Build the PROPOSE turn's user prompt: the entry just written, the pack's
    operator-authored guidance, the tool schemas, and the legal warrant
    vocabulary — this entry's OWN resolved refs. The pack rule is "cite only
    UUIDs your read tools returned"; handing the model exactly those is the
    anti-fabrication anchor."""
    lines = [
        "YOU HAVE JUST WRITTEN THIS ENTRY:",
        "",
        body.strip(),
        "",
        "Now — and only now, having reasoned it through — consider whether "
        "anything in it warrants a PROPOSAL. A proposal is a suggestion "
        "queued for a human to review; it changes NOTHING by itself, and it "
        "is never a fact write.",
    ]
    frags = [str(f).strip() for f in (write_fragments or []) if str(f).strip()]
    if frags:
        lines.append("")
        lines.extend(frags)
    lines += ["", "Available proposal tools:"] + [
        "  - " + _JOURNAL_PROPOSE_TOOL_SCHEMAS.get(
            name, f"{name}(...) — journal propose tool (see persona)."
        )
        for name in JOURNAL_PROPOSE_TOOLS
    ]
    if cited_refs:
        lines += ["", (
            "Refs this entry actually resolved (the ONLY UUIDs you may put in "
            "cited_substrate_refs — never invent one): "
            + ", ".join(str(r) for r in cited_refs[:_PROPOSE_REF_ECHO_CAP])
        )]
    lines += ["", (
        "Reply with EITHER a single strict-JSON tool call — "
        '{"tool": "<name>", "args": {"rationale": "...", "diff": {...}, '
        '"cited_substrate_refs": ["..."]}} — OR, if nothing warrants one, '
        'exactly {"propose": false}. Most entries warrant nothing; declining '
        "is the normal answer and costs you nothing."
    )]
    return "\n".join(lines)


async def _propose_phase(
    deps: InlineTargetDeps,
    *,
    body: str,
    cited_refs: list[UUID],
    analyst_id: str | None,
    tool_bindings: Mapping[str, Any],
    write_fragments: Any,
    steps: list[dict[str, Any]],
) -> dict[str, int]:
    """Offer the journal_propose pack at the ONE moment the model has a formed
    judgment: right after its entry is final and its citations are bound.

    Every admitted call runs through the pack's OWN per-run binding out of
    ``options['gather_tool_bindings']`` — the object the actor built via
    ``_gather_write_bindings_for_target`` (META self-allow + WritebackContext),
    the same one GATHER routes write tools through. No second dispatch path, no
    hand-built allow: an unbound propose tool is a LOUD no-op, never an
    ungoverned write. DEGRADE-NOT-DROP throughout — an LLM error, unparsable
    reply, or blocked/failing tool must never fail a run whose entry is already
    written. Returns the phase's token usage for the caller to fold.
    """
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    from .inline_target import _extract_json

    messages: list[Mapping[str, Any]] = [
        {
            "role": "user",
            "content": _propose_phase_prompt(
                body=body, cited_refs=cited_refs, write_fragments=write_fragments
            ),
        }
    ]
    queued = 0
    for round_idx in range(_PROPOSE_MAX_ROUNDS):
        try:
            content, usage = await _reason_via_llm(
                deps.llm,
                user_prompt="",
                max_tokens=deps.max_tokens,
                temperature=deps.temperature,
                system_prompt=deps.system_prompt,
                messages=messages,
            )
        except Exception as exc:  # degrade-not-drop — the entry is already written
            logger.warning(
                "journal_assessor.propose.llm_failed analyst_id=%s round=%d err=%s",
                analyst_id, round_idx + 1, exc,
            )
            steps.append({"phase": "propose", "kind": "llm_error", "round": round_idx + 1})
            break
        for k in usage_total:
            usage_total[k] += usage.get(k, 0)
        parsed = _extract_json(content or "")
        tool_name = str(parsed.get("tool")) if isinstance(parsed, dict) else ""
        if tool_name not in JOURNAL_PROPOSE_TOOLS:
            # The normal, expected ending: nothing warranted a proposal (or the
            # model said something that is not a proposal, which means the same).
            steps.append({
                "phase": "propose",
                "kind": "declined",
                "round": round_idx + 1,
                "queued": queued,
            })
            break
        binding = tool_bindings.get(tool_name)
        if binding is None:
            # Granted-and-catalogued but unbound: the pack was shown and cannot
            # be called. Loud, because it means the host/actor binding legs
            # disagree with the prompt surface — the exact silent-bypass shape
            # this whole phase exists to stop being invisible.
            logger.warning(
                "journal_assessor.propose.unbound analyst_id=%s tool=%s — the "
                "propose catalog was shown but no binding was wired for it",
                analyst_id, tool_name,
            )
            steps.append({
                "phase": "propose", "kind": "unbound", "tool": tool_name,
                "round": round_idx + 1,
            })
            break
        tool_args = parsed.get("args") or {}
        if not isinstance(tool_args, Mapping):
            tool_args = {}
        admitted = False
        detail: str = ""
        try:
            outcome = await binding.run_tool(tool_name, dict(tool_args))
            admitted = bool(outcome.admitted)
            if not admitted:
                detail = f"blocked: {outcome.block_cause}"
            elif outcome.tool_result is None or outcome.tool_result.status == "failed":
                admitted = False
                detail = (
                    f"failed: {outcome.tool_result.error}"
                    if outcome.tool_result is not None
                    else "failed: tool produced no result"
                )
        except Exception as exc:  # degrade-not-drop
            detail = f"failed: {exc!s}"
        steps.append({
            "phase": "propose",
            "kind": "tool_call",
            "round": round_idx + 1,
            "tool": tool_name,
            "admitted": admitted,
            **({"detail": detail} if detail else {}),
        })
        if admitted:
            queued += 1
            logger.info(
                "journal_assessor.propose.queued analyst_id=%s tool=%s queued=%d "
                "(pending human review — no live write)",
                analyst_id, tool_name, queued,
            )
        if queued >= _PROPOSE_MAX_PER_RUN:
            steps.append({"phase": "propose", "kind": "per_run_cap", "queued": queued})
            break
        messages = messages + [
            {"role": "assistant", "content": content or ""},
            {
                "role": "tool",
                "name": tool_name,
                "content": json.dumps(
                    {"queued": admitted, "detail": detail or "pending human review"}
                ),
            },
            {"role": "user", "content": _PROPOSE_DECLINE_INSTRUCTION.strip()},
        ]
    else:
        steps.append({"phase": "propose", "kind": "rounds_exhausted", "queued": queued})
    return usage_total


async def _narrate_with_tools(
    deps: InlineTargetDeps,
    *,
    field_notes: str,
    binding: Any,
    analyst_id: str | None,
    steps: list[dict[str, Any]],
) -> tuple[str, dict[str, int]]:
    """Run the narrate stage with tools LIVE (§4.4). The agent writes the entry;
    if it emits a tool call instead, we run it (through the SAME governed
    journal_read binding), fold the result back, and ask again for the entry —
    capped at ``_NARRATE_MAX_TOOL_ROUNDS``. When tools aren't bound (no binding),
    or the cap is hit, this is a closed-book narrate over the field notes (the
    §4.4 fallback). Every completion that is about to become the returned
    entry body first clears :func:`_guard_against_tool_call_leak` (task #236)
    — a completion that IS a legitimate, recognized tool call never reaches
    that guard; it is executed instead. Returns ``(entry_body, usage)``.
    """
    from .inline_target import _extract_json

    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}
    narrate_instruction = _NARRATE_INSTRUCTION_FOR.get(
        _entry_kind_for_analyst(analyst_id), _NARRATE_INSTRUCTION
    )
    messages: list[Mapping[str, Any]] = [
        {"role": "user", "content": field_notes + narrate_instruction}
    ]
    last_content = ""
    for round_idx in range(_NARRATE_MAX_TOOL_ROUNDS):
        try:
            # PER-PHASE LLM SPLIT (§4.1): NARRATE runs on the narrate handler
            # (Opus when split; primary handler otherwise). The mid-entry tool
            # call still routes through the journal_read GATHER binding below —
            # only the LLM authoring the prose changes, not the tool plane.
            content, usage = await _reason_via_llm(
                deps.narrate_llm(),
                user_prompt="",
                max_tokens=deps.narrate_tokens(),
                temperature=deps.temperature,
                system_prompt=deps.system_prompt,
                messages=messages,
            )
        except Exception:
            steps.append({"phase": "narrate", "kind": "llm_error", "round": round_idx + 1})
            raise
        for k in usage_total:
            usage_total[k] += usage.get(k, 0)
        last_content = content or ""
        # Did the agent ask to pull one more thread? Only honor a tool call when a
        # binding is wired and we have rounds left.
        parsed = _extract_json(content) if binding is not None else None
        tool_name = str(parsed.get("tool")) if isinstance(parsed, dict) else ""
        if (
            binding is not None
            and tool_name in JOURNAL_READ_TOOLS
            and round_idx < _NARRATE_MAX_TOOL_ROUNDS - 1
        ):
            tool_args = parsed.get("args") or {}
            if not isinstance(tool_args, Mapping):
                tool_args = {}
            try:
                outcome = await binding.run_tool(tool_name, dict(tool_args))
                if not outcome.admitted:
                    tool_result: dict[str, Any] = {
                        "error": f"tool_blocked: {outcome.block_cause}"
                    }
                elif outcome.tool_result is None or outcome.tool_result.status == "failed":
                    err = (
                        outcome.tool_result.error
                        if outcome.tool_result is not None
                        else "tool produced no result"
                    )
                    tool_result = {"error": f"tool_failed: {err}"}
                else:
                    tool_result = dict(outcome.tool_result.output)
            except Exception as exc:  # degrade-not-drop
                tool_result = {"error": f"tool_failed: {exc!s}"}
            steps.append({
                "phase": "narrate",
                "kind": "tool_call",
                "round": round_idx + 1,
                "tool": tool_name,
                "ok": "error" not in tool_result,
            })
            messages = messages + [
                {"role": "assistant", "content": content},
                {
                    "role": "tool",
                    "name": tool_name,
                    # R2 / W2-T3: JSON-safe cut + explicit truncated marker.
                    "content": _bounded_tool_json(tool_result, 4000),
                },
                {
                    "role": "user",
                    "content": (
                        "Now write your journal entry as plain markdown prose, "
                        "citing the refs inline."
                    ),
                },
            ]
            continue
        # The agent wrote the entry (not a tool call) — done. Task #236: clear
        # the tool-call-leak guard BEFORE this becomes the returned body (a
        # completion already routed to the tool-execution branch above is a
        # legitimate EXECUTED call and never reaches this point).
        last_content = await _guard_against_tool_call_leak(
            deps,
            content=last_content,
            messages=messages,
            usage_total=usage_total,
            steps=steps,
        )
        steps.append({
            "phase": "narrate",
            "kind": "entry_written",
            "round": round_idx + 1,
            "body_chars": len(last_content),
        })
        return last_content.strip(), usage_total
    # Cap hit with no clean entry — use whatever the last turn produced. Same
    # guard as the entry_written path (task #236) — a rounds-exhausted
    # completion becomes the body exactly the same way.
    last_content = await _guard_against_tool_call_leak(
        deps,
        content=last_content,
        messages=messages,
        usage_total=usage_total,
        steps=steps,
    )
    steps.append({"phase": "narrate", "kind": "rounds_exhausted"})
    return last_content.strip(), usage_total


# ---------------------------------------------------------------------------
# §10 The DETERMINISTIC honesty post-step — forced from substrate metrics, NEVER
# trusted as agent-self-reported. The agent that over-narrates forecast skill is
# exactly the agent that would omit ``forecast_unproven``, so a self-reported flag
# is a circular guard.
# ---------------------------------------------------------------------------


async def _forced_honesty_flags(
    binding: Any,
    *,
    steps: list[dict[str, Any]],
) -> list[str]:
    """Read the live calibration posture from the substrate (via the governed
    ``get_calibration`` instrument) and FORCE the honesty flags it implies,
    regardless of what the narrative said (§10).

    The ``get_calibration`` port method computes ``forecast_unproven`` /
    ``calibration_thin`` deterministically from the real Brier / BSS / sample-size
    state, so this post-step just reads those verdicts. Degrade-not-drop: when no
    binding is wired or the read fails, we conservatively flag BOTH legs as
    unproven (the honest default — absence of proof is not proof of skill). When
    calibration data is genuinely present and a leg IS proven, the corresponding
    flag is omitted.
    """
    if binding is None:
        steps.append({"phase": "honesty", "kind": "no_binding_conservative"})
        return ["forecast_unproven", "calibration_thin"]
    try:
        outcome = await binding.run_tool("get_calibration", {})
        if (
            not outcome.admitted
            or outcome.tool_result is None
            or outcome.tool_result.status == "failed"
        ):
            steps.append({"phase": "honesty", "kind": "read_failed_conservative"})
            return ["forecast_unproven", "calibration_thin"]
        data = dict(outcome.tool_result.output)
    except Exception as exc:  # degrade-not-drop
        logger.warning("journal_assessor.honesty.read_failed err=%s", exc)
        steps.append({"phase": "honesty", "kind": "read_error_conservative"})
        return ["forecast_unproven", "calibration_thin"]
    flags: list[str] = []
    # The port computes these verdicts deterministically (forecast leg proven only
    # when ready + non-degenerate + BSS>0; calibration thin when exogenous n<5).
    if data.get("forecast_unproven", True):
        flags.append("forecast_unproven")
    if data.get("calibration_thin", True):
        flags.append("calibration_thin")
    steps.append({
        "phase": "honesty",
        "kind": "forced_from_substrate",
        "flags": list(flags),
        "calibration_available": bool(data.get("available")),
    })
    return flags


async def _source_health_cross_check(
    binding: Any,
    inputs: list[dict[str, Any]],
    *,
    steps: list[dict[str, Any]],
) -> list[str]:
    """j6 review #3 — the DETERMINISTIC instrument-vs-gather cross-check.

    If ``get_source_health`` reports fewer ACTIVE sources than the number of
    DISTINCT sources that actually delivered signals into this run's priming
    slice, the instrument is showing a filtered/truncated view — fail LOUD with
    a forced ``source_health_inconsistent`` honesty flag (never trust either
    side silently). Degrade-not-drop: no binding / read failure → no flag (the
    narrator's other disciplines still apply); the check never blocks a run."""
    # Count only REAL signal rows (id non-None): the META reader appends
    # graph-structure context rows with source_id='graph_metrics' and id=None —
    # a pseudo-source that must not inflate the count (review C).
    distinct = len({
        str(r.get("source_id"))
        for r in inputs
        if r.get("source_id") is not None and r.get("id") is not None
    })
    if binding is None or distinct == 0:
        return []
    try:
        outcome = await binding.run_tool("get_source_health", {})
        if (
            not outcome.admitted
            or outcome.tool_result is None
            or outcome.tool_result.status == "failed"
        ):
            return []
        data = dict(outcome.tool_result.output)
    except Exception as exc:  # degrade-not-drop
        logger.warning("journal_assessor.source_check.read_failed err=%s", exc)
        return []
    summary = data.get("summary") if isinstance(data.get("summary"), Mapping) else {}
    try:
        total_wired = int(summary.get("total_wired"))
    except (TypeError, ValueError):
        return []
    # Compare against TOTAL WIRED, not active_total: a source paused or retired
    # MID-WINDOW (the trio pause, a re-auth) legitimately delivered signals
    # while no longer counting as active — delivered ⊆ wired is the invariant
    # that holds (review C; the only violation is a hard descriptor delete,
    # where flagging IS honest).
    if total_wired < distinct:
        logger.warning(
            "journal_assessor.source_check.INCONSISTENT total_wired=%d < "
            "distinct_gather_sources=%d — instrument shows a filtered view",
            total_wired, distinct,
        )
        steps.append({
            "phase": "honesty",
            "kind": "source_health_inconsistent",
            "total_wired": total_wired,
            "distinct_gather_sources": distinct,
        })
        return ["source_health_inconsistent"]
    steps.append({
        "phase": "honesty",
        "kind": "source_health_consistent",
        "total_wired": total_wired,
        "distinct_gather_sources": distinct,
    })
    return []


# ---------------------------------------------------------------------------
# S-1 (2026-07-27 sweep, SWEEP_SYNTHESIS §T1-#1 / SWEEP_SOURCES_SIGNALS §2) — the
# DETERMINISTIC source-health NUMBER guard. The sibling _source_health_cross_check
# validates the INSTRUMENT against the gather slice (total_wired ≥ delivered); it
# NEVER reads the prose, and it no-ops on the lens tier (whose priming slice
# carries no real signal rows, so distinct == 0). A faculty lens FABRICATED the
# collection posture ("0 active feeds … the window is dark") while
# get_source_health returned the correct 68/49/37 on every call — and it shipped
# UNFLAGGED. This guard closes that hole: it EXTRACTS the whole-fleet source-health
# COUNTS the narrator actually wrote and compares them against the live
# get_source_health `summary`. On divergence it forces `source_health_fabricated`
# — ANNOTATES, never rewrites the prose (the journal is contain-not-block; honesty
# = flag, never strip). Runs on EVERY tier, so the lens path is covered where its
# sibling is not.
# ---------------------------------------------------------------------------

_SOURCE_HEALTH_FABRICATED_FLAG = "source_health_fabricated"

# Only the STRUCTURAL, within-cycle-STATIC fields are strictly validated:
#   * total_wired  — count of head descriptors (state-driven; changes only when a
#     new descriptor version lands, ~daily, never mid-run).
#   * active_total — head descriptors WHERE state='active' (same driver).
# active_fresh / active_stalled / active_erroring are DELIBERATELY excluded: they
# key on the rolling 48h signal window + last-poll outcome and legitimately DRIFT
# between the narrator's tool call and this compose-time re-call, so a strict
# equality check on them would false-positive on ordinary freshness churn. The
# structural pair already pins every observed fabrication ("0 active feeds / the
# window is dark"; "active_total = 3 … total_wired = 3") — a dark-window claim
# necessarily corrupts the structural counts. Each pattern captures ONE integer.
_SOURCE_HEALTH_KEY_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    # The explicit `field = N` / `field: N` forms are UNAMBIGUOUS whole-fleet
    # claims — those literal summary field names ARE the denominator-honest
    # aggregate, so they carry no subset scope and need no scope guard.
    "active_total": (re.compile(r"\bactive_total\s*[=:]\s*(\d+)", re.IGNORECASE),),
    "total_wired": (re.compile(r"\btotal_wired\s*[=:]\s*(\d+)", re.IGNORECASE),),
}

# Natural-language forms — the exact register the fabrications used ("49 active
# feeds", "58 total wired", "total wired = 68"). These DO carry the scope guard: a
# properly-declared subset ("of the press-class subset, 3 active feeds") is the
# persona's sanctioned "any subset count NAMES its scope" and must be exempt.
_SOURCE_HEALTH_NL_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "active_total": (
        re.compile(r"(\d+)\s+active\s+(?:feeds?|sources?)\b", re.IGNORECASE),
    ),
    "total_wired": (
        re.compile(r"(\d+)\s+total\s+wired\b", re.IGNORECASE),
        re.compile(r"\btotal\s+wired\s*[=:]?\s*(\d+)", re.IGNORECASE),
    ),
}

# A subset scope cue immediately BEFORE a natural-language count exempts it (the
# narrator declared a scope, so the number is not a whole-fleet claim).
_SOURCE_HEALTH_SCOPE_CUE_RE = re.compile(
    r"(?:of\s+the|of\s+its|of\s+\d|among|within|subset|press[- ]class|"
    r"per[- ]class|this\s+window)",
    re.IGNORECASE,
)


def _extract_source_health_claims(body: str) -> dict[str, set[int]]:
    """PURE helper (no LLM, no binding): pull the whole-fleet source-health COUNTS
    the narrator wrote, keyed to the get_source_health ``summary`` fields.

    Explicit ``field = N`` forms are always taken (that literal field name is the
    whole-fleet aggregate). Natural-language forms ("N active feeds", "N total
    wired") are skipped when immediately preceded by a subset scope cue — a NAMED
    subset carries its own count by the persona's contract and is not validated
    against the fleet total. Returns ``{field: {claimed values}}`` (a set so a
    repeated identical claim collapses)."""
    claims: dict[str, set[int]] = {}
    if not body:
        return claims
    for field, patterns in _SOURCE_HEALTH_KEY_PATTERNS.items():
        for pat in patterns:
            for m in pat.finditer(body):
                claims.setdefault(field, set()).add(int(m.group(1)))
    for field, patterns in _SOURCE_HEALTH_NL_PATTERNS.items():
        for pat in patterns:
            for m in pat.finditer(body):
                window = body[max(0, m.start() - 30):m.start()]
                if _SOURCE_HEALTH_SCOPE_CUE_RE.search(window):
                    continue  # a declared subset — scope named, exempt
                claims.setdefault(field, set()).add(int(m.group(1)))
    return claims


async def _source_health_number_check(
    binding: Any,
    body: str,
    *,
    steps: list[dict[str, Any]],
) -> list[str]:
    """S-1 — the DETERMINISTIC prose-vs-instrument NUMBER cross-check.

    Extract the whole-fleet source-health counts the narrator WROTE and compare
    them against the live ``get_source_health`` ``summary``. On ANY divergence
    force ``source_health_fabricated`` (ANNOTATE, never rewrite the prose — honesty
    = flag, never strip). Degrade-not-drop: no binding / no numeric claim / read
    failure / missing summary → no flag (the run never blocks). Runs on EVERY
    tier, so the LENS path is covered — its sibling ``_source_health_cross_check``
    no-ops there (the lens priming slice has no real signal rows, distinct == 0)."""
    claims = _extract_source_health_claims(body)
    if binding is None or not claims:
        return []
    try:
        outcome = await binding.run_tool("get_source_health", {})
        if (
            not outcome.admitted
            or outcome.tool_result is None
            or outcome.tool_result.status == "failed"
        ):
            return []
        data = dict(outcome.tool_result.output)
    except Exception as exc:  # degrade-not-drop
        logger.warning(
            "journal_assessor.source_number_check.read_failed err=%s", exc
        )
        return []
    summary = data.get("summary") if isinstance(data.get("summary"), Mapping) else {}
    if not summary:
        return []
    mismatches: list[dict[str, Any]] = []
    for field, claimed_values in claims.items():
        if field not in summary:
            continue
        try:
            actual = int(summary[field])
        except (TypeError, ValueError):
            continue
        for claimed in sorted(claimed_values):
            if claimed != actual:
                mismatches.append(
                    {"field": field, "claimed": claimed, "actual": actual}
                )
    if mismatches:
        logger.warning(
            "journal_assessor.source_number_check.FABRICATED mismatches=%s — "
            "narrated source-health counts diverge from get_source_health",
            mismatches,
        )
        steps.append({
            "phase": "honesty",
            "kind": "source_health_fabricated",
            "mismatches": mismatches,
        })
        return [_SOURCE_HEALTH_FABRICATED_FLAG]
    steps.append({
        "phase": "honesty",
        "kind": "source_health_numbers_consistent",
        "checked_fields": sorted(claims.keys()),
    })
    return []


# ---------------------------------------------------------------------------
# VOICES LV-1 — the chorus DIFF matrix (§3.3). The DETERMINISTIC part: the roster
# (which faculties were seen this cycle, which are missing) computed BEFORE
# narrate and stamped into ``data.matrix``, mirroring _forced_honesty_flags's
# "compute facts, hand to NARRATE" pattern. Topic ALIGNMENT is deliberately NOT
# automated in v1 — NARRATE (reading all four bodies) does it as an LLM judgment
# (the tractable question; §3.3), and the rich per-topic breakdown lives in the
# prose body. The structured roster here lets a reader/UI see the aperture (an
# absent faculty) join-free, and lets the honest partial-roster contract (§3.4)
# be a fact in the data, not only in the prose.
# ---------------------------------------------------------------------------


def _lens_diff_roster_from_reads(reads: list[dict[str, Any]]) -> dict[str, Any]:
    """PURE helper (no LLM, no binding): fold this cycle's raw lens reads into the
    deterministic diff-matrix roster.

    Keeps only the MOST RECENT read per ``analyst_id`` (idempotent under a
    retry/double-fire — a stale duplicate is dropped by produced_at), and reports
    ``analyst_ids_seen`` (of the 4 v1 faculties) + ``analyst_ids_missing`` so
    NARRATE can be honest about an absent faculty rather than silently thinning
    the matrix. ``topics`` is left EMPTY — the LLM fills the topic alignment into
    the prose body; v1 does not cluster deterministically."""
    latest: dict[str, dict[str, Any]] = {}
    for r in reads or []:
        if not isinstance(r, Mapping):
            continue
        aid = r.get("analyst_id")
        if not isinstance(aid, str) or aid not in LENS_ANALYST_IDS:
            continue
        prev = latest.get(aid)
        if prev is None or _slice_recency_key(r) > _slice_recency_key(prev):
            latest[aid] = dict(r)
    seen = [aid for aid in LENS_ANALYST_IDS if aid in latest]
    missing = [aid for aid in LENS_ANALYST_IDS if aid not in latest]
    return {
        "topics": [],  # NARRATE aligns topics into the prose body (§3.3)
        "analyst_ids_seen": seen,
        "analyst_ids_missing": missing,
    }


async def _compute_lens_diff_matrix(
    binding: Any,
    *,
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Read this cycle's four faculty reads via the governed ``get_lens_reads``
    instrument and compute the deterministic roster (§3.3). Degrade-not-drop: no
    binding / read failure → an all-missing roster (honest: we could not confirm
    any faculty ran), never a crash. Mirrors _forced_honesty_flags's direct
    ``binding.run_tool`` read."""
    if binding is None:
        steps.append({"phase": "diff", "kind": "no_binding_all_missing"})
        return _lens_diff_roster_from_reads([])
    try:
        outcome = await binding.run_tool("get_lens_reads", {})
        if (
            not outcome.admitted
            or outcome.tool_result is None
            or outcome.tool_result.status == "failed"
        ):
            steps.append({"phase": "diff", "kind": "read_failed_all_missing"})
            return _lens_diff_roster_from_reads([])
        data = dict(outcome.tool_result.output)
    except Exception as exc:  # degrade-not-drop
        logger.warning("journal_assessor.diff.read_failed err=%s", exc)
        steps.append({"phase": "diff", "kind": "read_error_all_missing"})
        return _lens_diff_roster_from_reads([])
    reads = data.get("reads")
    matrix = _lens_diff_roster_from_reads(reads if isinstance(reads, list) else [])
    steps.append({
        "phase": "diff",
        "kind": "matrix_computed",
        "analyst_ids_seen": list(matrix["analyst_ids_seen"]),
        "analyst_ids_missing": list(matrix["analyst_ids_missing"]),
    })
    return matrix


# ---------------------------------------------------------------------------
# V2.2 — the DETERMINISTIC apparatus-lead honesty flag (MASTER_PLAN 2026-07-10
# §REVIEW 2026-07-23 "JOURNAL R-3 PROOF: MIXED"). The persona already carries the
# HARD world-first opening rule; when the narrator regresses anyway ("I start by
# checking the health of my senses…") this ANNOTATES the entry — never blocks,
# never rewrites (the diary is contain-not-block by design). Mirrors the idempotent
# array_append semantics of actor_critic._stamp_journal_contradicted_flag, but
# runs at COMPOSE time alongside the sibling forced honesty flags (the lead is a
# pure function of the body — no post-insert DB round-trip needed).
# ---------------------------------------------------------------------------

_APPARATUS_LEAD_FLAG = "apparatus_lead"

# CONSERVATIVE apparatus-facing opening phrases. Deliberately anchored to the
# self/instrument register the persona bans as a LEAD (checking senses / opening
# the dashboard / feed-health inventory / ingestion posture), NOT ordinary world
# prose that merely mentions a source. Kept tight to avoid false positives — the
# apparatus is a LEGAL closing note, so only a first-sentence apparatus OPENING
# trips this. Each alternative pairs an apparatus subject with a checking verb or
# a health/inventory framing.
_APPARATUS_LEAD_RE = re.compile(
    r"(?:"
    # "I start by checking … my senses/pipeline/feeds…" — the start/begin/open-by
    # framing ONLY when an apparatus subject follows within the sentence (a bare
    # "start with a handshake in Geneva" is a WORLD lead, never flagged).
    r"\b(?:start|begin|open)\s+(?:by|with)\b[^.]{0,60}?"
    r"\b(?:sens|feed|source|pipeline|ingest|dashboard|instrument|apparatus|"
    r"health\s+(?:of|dashboard)|poll|plumbing)"
    # a checking/surveying verb aimed at the apparatus register
    r"|\b(?:check|checking|checked|open|opened|opening|survey|surveying|review|"
    r"reviewing|glance\s+at|look(?:ing)?\s+at|take\s+stock\s+of|inspect|inspecting)"
    r"\b[^.]{0,60}?"
    r"\b(?:sens|feed|source|pipeline|ingest|dashboard|instrument|apparatus|"
    r"health|poll|plumbing)"
    # "the health of my senses/feeds/sources/pipeline"
    r"|\bhealth\s+of\s+(?:my|the)\s+(?:sens|feed|source|pipeline|ingest|instrument)"
    # "my senses/feeds/pipeline (are|were|show) …" as the very first move
    r"|\b(?:my|the)\s+(?:senses|feeds|sources|pipeline|ingestion|instruments)\b"
    r"[^.]{0,40}?\b(?:are|were|show|report|look|seem|come\s+up)\b"
    r")",
    re.IGNORECASE,
)


def _lead_text(body: str) -> str:
    """The entry's OPENING window for the apparatus-lead check: the first
    sentence(s) of the first non-empty paragraph, capped so a long paragraph
    can't drag a mid-body apparatus mention into the "lead". A leading markdown
    title line ("# …" / "**…**") is stripped first — the title is not the lead."""
    if not body:
        return ""
    # Drop a leading ATX / bold-only title line so the real opening prose is the
    # lead (mirrors _derive_title's title shapes; only a LEADING title is dropped).
    stripped = _TITLE_LINE_RE.sub("", body, count=1)
    stripped = _BOLD_TITLE_RE.sub("", stripped, count=1)
    para = ""
    for chunk in re.split(r"\n\s*\n", stripped):
        if chunk.strip():
            para = chunk.strip()
            break
    if not para:
        return ""
    # First 1-2 sentences (up to the 2nd sentence terminator), hard-capped.
    sentences = re.split(r"(?<=[.!?])\s+", para)
    lead = " ".join(sentences[:2])
    return lead[:400]


def _apparatus_lead_flag(body: str) -> list[str]:
    """Return ``[_APPARATUS_LEAD_FLAG]`` iff the entry OPENS apparatus-facing
    (the persona's banned "checking my senses" lead), else ``[]``.

    Conservative + deterministic: only the first sentence(s) are examined (the
    apparatus is a legal CLOSING note, so a later mention never trips this), and
    the phrase set is anchored to the self/instrument register — ordinary world
    prose that merely names a source is not flagged. ANNOTATES ONLY."""
    lead = _lead_text(body)
    if lead and _APPARATUS_LEAD_RE.search(lead):
        return [_APPARATUS_LEAD_FLAG]
    return []


# ---------------------------------------------------------------------------
# QW1-D fix 1/2 — the GATHER tool catalog, DERIVED FROM THE GRANT (never
# hand-listed). planning/prompt_gallery/p3_journal_family.md §1/§8: the journal
# family used to reuse inline_target's GENERIC ``_gather_system_suffix`` — a
# catalog of 12 substrate_read-shaped tools (only 5 real for 7 of the 8
# classes, since the journal is granted ``journal_read``, not
# ``substrate_read`` — journal_read.py's module docstring). The journal's OWN
# 10 self-instruments (get_assessments, get_source_health, get_calibration, …)
# never appeared in that catalog at all — the model learned their names only
# from scattered persona/user-prompt prose, with zero formal arg schema. The
# entry/consolidation WRITE-BACK suffix was WORSE: it hardcoded the generic
# ``propose_facts`` pack's tools (propose_fact/request_source/open_question) —
# tools belonging to a pack the journal is NEVER granted — immediately before
# describing the REAL journal_propose tools, with no disambiguation.
#
# This catalog is built from the SAME two tuples the four-surface drift guard
# already treats as the single source of truth (JOURNAL_READ_TOOLS /
# JOURNAL_PROPOSE_TOOLS, tests/runtime/test_journal_assessor_wiring.py): a
# tool absent from these tuples can never appear here, and a tool ADDED to
# them appears automatically (with a generic fallback line) even before
# anyone authors a dedicated one-liner for it. The one-line purpose + arg
# schema per name is the one HAND-AUTHORED surface (prose has to come from
# somewhere) — the SET of tools shown is never hand-listed.
_JOURNAL_READ_TOOL_SCHEMAS: dict[str, str] = {
    "list_findings": (
        "list_findings([target_id], [analyst_id], [severity], [since_hours], "
        "[include_superseded], [limit]) — the platform's own prior LIVE "
        "findings/assessments; cite the output_id."
    ),
    "query_facts": (
        "query_facts([subject], [predicate], [value], [limit]) — the current "
        "temporal fact store."
    ),
    "query_nexuses": (
        "query_nexuses([subject], [object], [rel_type], [polarity], [limit]) "
        "— open signed/typed relationships."
    ),
    "list_situations": (
        "list_situations([status], [target_id], [since_hours], [limit]) — "
        "ongoing first-class situation frames."
    ),
    "get_timeline": (
        "get_timeline(subject, [limit]) — time-ordered facts ∪ signals for "
        "one subject."
    ),
    # B-8: the producer list is DERIVED, never typed here. On 2026-08-03 this
    # line read "recent country_assessor/world_assessor reads" — naming an
    # analyst that has been state='draft' and silent for months. The planner did
    # exactly as told, passed analyst_id='country_assessor', got zero rows, and
    # the entry narrated "the assessment engine produced no country-level rows in
    # the last 48 hours" on a day with 1,562 successful runs and 131 fresh
    # country_composition findings. A hand-typed roster in a prompt is a fact
    # about the fleet with no mechanism keeping it true; derived, it cannot rot.
    "get_assessments": (
        "get_assessments([analyst_id], [target_id], [since_hours=48], "
        "[limit=20]) — recent LIVE assessment reads. Omit analyst_id to see "
        "the whole live surface (that is almost always what you want); the "
        "live producers are: " + ", ".join(_ASSESSMENT_PRODUCER_ANALYSTS) + ". "
        "Also carries a `disagreements` block where a banded scorecard excluded "
        "a dimension the live composition still cites — null there means NOT "
        "MEASURED, which is not the same as agreement."
    ),
    "get_graph_structure": (
        "get_graph_structure([limit=20]) — graph_mining communities/"
        "centrality over the knowledge graph."
    ),
    "get_structural_balance": (
        "get_structural_balance([limit=20]) — unstable (++−) signed-nexus "
        "triads (a prediction of tension, not a settled fact)."
    ),
    "get_critic_scores": (
        "get_critic_scores([analyst_id], [since_hours=168], [limit=20]) — "
        "the critic's rubric scores (NON-ACTUATING — reading it is "
        "reflection, not a closed loop)."
    ),
    "get_calibration": (
        "get_calibration() — forecast/calibration tracking, incl. the "
        "segregated brier_forecast_acute pilot (n<30, no proven skill yet)."
    ),
    "get_run_health": (
        "get_run_health([analyst_id], [quiet_hours=24], [limit=200]) — what "
        "fired vs went quiet across the analyst fleet."
    ),
    "get_source_health": (
        "get_source_health([silent_only=False], [silent_hours=48], "
        "[limit=200]) — source poll outcomes; its `summary` block carries "
        "the honest denominator (total_wired / by_state / active_fresh / "
        "active_stalled / active_erroring) — speak THAT, never the capped "
        "`rows` list."
    ),
    "get_budget_status": (
        "get_budget_status([analyst_id], [demotion_lookback_hours=168], "
        "[limit=40]) — governor/budget pressure across the fleet."
    ),
    "get_journal_delta": (
        "get_journal_delta([since], [limit=30]) — what changed since your "
        "last entry, and your own prior entry/consolidation (memory, never "
        "the sole ref for a fact claim)."
    ),
    "get_lens_reads": (
        "get_lens_reads() — this cycle's four VOICES faculty lens reads "
        "(inert except for the lens_diff chorus pass)."
    ),
}

_JOURNAL_PROPOSE_TOOL_SCHEMAS: dict[str, str] = {
    "propose_correction": (
        "propose_correction(rationale, diff, [cited_substrate_refs]) — "
        "propose a correction (a stale fact to supersede / an entity merge / "
        "a situation fix). Queues ONE journal_proposals row; NEVER a live "
        "write."
    ),
    "propose_change": (
        "propose_change(rationale, diff, [cited_substrate_refs]) — propose a "
        "descriptor/config change. Queues ONE journal_proposals row; NEVER a "
        "live write."
    ),
    "propose_self_revision": (
        "propose_self_revision(rationale, diff, [cited_substrate_refs]) — "
        "propose a diff to YOUR OWN system prompt (the highest-scrutiny "
        "class — protected sections auto-reject at accept time). Queues ONE "
        "journal_proposals row; NEVER a direct self-edit."
    ),
}


def _journal_gather_catalog(*, granted_propose: bool) -> str:
    """Build the journal-family GATHER tool catalog FROM the granted pack
    tuples — ``JOURNAL_READ_TOOLS`` always (every journal-family class grants
    ``journal_read``), ``JOURNAL_PROPOSE_TOOLS`` iff ``granted_propose``
    (true only when the running class's ``journal_propose`` pack is actually
    bound this run — entry + consolidation today, per ``run_method``'s
    ``write_fragments`` check). Never hand-listed: a tool absent from either
    tuple can never appear; a tool present but missing an authored schema
    line falls back to a generic one-liner rather than silently vanishing
    (QW1-D fix 1 + fix 2)."""
    lines = [
        "\n\nBefore you write the entry you may FIRST query the substrate + "
        "your own instruments to ground your reflection. Each query must be "
        "a single strict-JSON object.\nAvailable tools:"
    ]
    for name in JOURNAL_READ_TOOLS:
        lines.append(
            "  - " + _JOURNAL_READ_TOOL_SCHEMAS.get(
                name, f"{name}(...) — journal read instrument (see persona)."
            )
        )
    if granted_propose:
        lines.append(
            "\nWRITE-BACK (journal_propose pack — these PROPOSE, they do NOT "
            "assert truth or mutate anything directly; a human always "
            "reviews before anything is applied):"
        )
        for name in JOURNAL_PROPOSE_TOOLS:
            lines.append(
                "  - " + _JOURNAL_PROPOSE_TOOL_SCHEMAS.get(
                    name, f"{name}(...) — journal propose tool (see persona)."
                )
            )
    lines.append(
        "\nProtocol:\n"
        '  - To query, reply with strict JSON: {"tool": "<name>", "args": {...}}\n'
        '  - When you have gathered enough, reply with: {"done": true}\n'
        "  - Do not write the entry yet — you will be asked for it after "
        "gathering."
    )
    return "\n".join(lines) + "\n"


async def run_method(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: InlineTargetDeps | LLMHandlerLike,
) -> AnalystMethodResult:
    """Execute one journal-entry run over the substrate (plan §4.2 staged arc).

    PLAN → GATHER (deep ReAct over the journal_read pack — the whole animal + its
    own instruments) → FIELD-NOTES seam (§4.3, in-voice cited handoff) → NARRATE
    (§4.4, tools live) → REFLECT (§10, permissive citation flag) → HONESTY (§10,
    deterministic flags forced from substrate). The persona is loaded in EVERY
    phase (the worldview is the attention mechanism). Emits a ``JournalPayload``
    and returns ``derived_from=[]`` (off the chain).
    """
    if not isinstance(deps, InlineTargetDeps):
        deps = InlineTargetDeps(llm=deps)

    analyst_id = options.get("analyst_id")
    # Wave 2 (§4.7): the tier is the descriptor — the consolidator id distills a
    # ``consolidation`` (the write path then fires supersede_prior_consolidation);
    # every other id appends an ``entry``. Pure-function of the analyst id; no
    # per-descriptor mode flag.
    entry_kind = _entry_kind_for_analyst(analyst_id)
    is_consolidation = entry_kind == "consolidation"
    steps: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    # The window the run reflects on. The entry tier reflects on the freshest
    # 24h; the consolidation tier folds a wider running window (its daily beat)
    # into the forward-carried inner landscape — get_journal_delta (in GATHER)
    # tells it where it left off (the prior consolidation + recent entries). A
    # delta-priming READ_SLICE narrowing this window is a later refinement.
    period_end = now
    # entry = the freshest 24h; consolidation folds a week; the chronicle
    # RECORDS a week; the VOICES lens / lens_diff tiers pair with the chronicle's
    # weekly window (DL-4) — all non-entry tiers fall into the 7d else branch, so
    # no lens-specific arm is needed here.
    period_start = now - timedelta(hours=24 if entry_kind == "entry" else 24 * 7)
    steps.append({"phase": "wake", "kind": "tier", "entry_kind": entry_kind})
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}

    def _fold(u: Mapping[str, int]) -> None:
        for k in usage:
            usage[k] += int(u.get(k, 0) or 0)

    steps.append({"phase": "wake", "kind": "envelope"})

    # --- PLAN / ORIENT -------------------------------------------------
    # Orient the journal to its OWN memory: get_journal_delta (its off-chain
    # entries + the recent_entries walk-back) is where its history lives; the
    # finding chain never holds it (2026-07-03 continuity fix). The VOICES tiers
    # orient to where the ARGUMENT stands (lens) / this cycle's reads (lens_diff).
    memory_orientation = _MEMORY_ORIENTATION_FOR.get(
        entry_kind, _MEMORY_ORIENTATION
    )
    # VOICES LV-1 (§2.4/§2.6): a faculty's declared prior is echoed into its user
    # prompt VERBATIM (the copy the verify judge/audit points at). Resolved from
    # the analyst id via the persona module — NOT threaded through options ("" for
    # every non-faculty tier, incl. the diff pass which has no prior of its own).
    lens_prior_block = _lens_prior_block_for(analyst_id)
    user_prompt = (
        _render_user_prompt(
            inputs, tier=entry_kind, lens_prior_block=lens_prior_block
        )
        + memory_orientation
    )
    # The logged prompt_module the descriptor resolves — one arm per tier.
    if is_consolidation:
        _logged_prompt_module = CONSOLIDATOR_PROMPT_MODULE_PATH
    elif entry_kind == "chronicle":
        _logged_prompt_module = CHRONICLE_PROMPT_MODULE_PATH
    elif entry_kind == "lens_diff":
        _logged_prompt_module = LENS_DIFF_PROMPT_MODULE_PATH
    elif entry_kind == "lens":
        _logged_prompt_module = LENS_PROMPT_MODULE_PATHS.get(
            analyst_id or "", PROMPT_MODULE_PATH
        )
    else:
        _logged_prompt_module = PROMPT_MODULE_PATH
    steps.append({
        "phase": "plan",
        "kind": "render_prompt",
        "in_count": len(inputs),
        "prompt_chars": len(user_prompt),
        "prompt_module": _logged_prompt_module,
    })

    # --- GROUND (Tier-1 knowledge grounding, if the descriptor opted in) ---
    if deps.grounding_hook is not None:
        try:
            preamble = await deps.grounding_hook(inputs, options)
        except Exception as exc:  # pragma: no cover — grounding must not fail the run
            logger.warning("journal_assessor.grounding.failed err=%s", exc)
            preamble = None
        if preamble:
            user_prompt = f"{preamble}\n{user_prompt}"
            steps.append({"phase": "ground", "kind": "inject_preamble"})

    # --- GATHER (deep ReAct over the journal_read pack — §5 the whole animal) ---
    active_binding = options.get("agency_binding") or deps.agency_binding
    # The journal's OWN instrument tool names are NOT in inline_target's
    # _GATHER_READ_TOOLS, so we pass them via ``extra_read_tools`` — they are then
    # recognized AND routed through the journal_read binding (§4.9).
    #
    # Wave 4 (§7): the journal ALSO grants the journal_propose pack — its
    # PROPOSE-AND-GATE write tools. Those names ride ``extra_write_tools`` (NOT
    # extra_read_tools) so they are recognized but route through their per-tool
    # binding in ``gather_tool_bindings`` (carrying the per-run WritebackContext),
    # exactly like the generic propose_facts write tools. The host builds that
    # binding iff the journal_propose pack is EFFECTIVE (granted); when it is
    # bound, ``options['gather_write_prompt_fragments']`` is non-None — that is
    # the SAME signal ``_journal_gather_catalog`` uses to decide whether to show
    # the journal's OWN propose_correction/propose_change/propose_self_revision
    # tools (never the generic, ungranted propose_fact/request_source/
    # open_question pack — QW1-D fix 1 + fix 2).
    write_fragments = options.get("gather_write_prompt_fragments")
    gather_system = _journal_gather_catalog(
        granted_propose=write_fragments is not None,
    )
    # V4 (GATHER [N]→journal bridge): the {N -> citation entry} map for the corpus
    # documents GATHER numbered. Populated only when GATHER engages + surfaced a
    # corpus reader (search_corpus / read_document); kept so the post-NARRATE
    # rewrite can turn a [N] the narrator wrote against a gathered doc into a
    # durable [[ref:uuid]] (J4 — otherwise the [N] dies at render).
    gather_citation_extension: dict[int, dict[str, Any]] = {}
    if active_binding is not None:
        # 5-tuple return (Piece 1 added ``citation_extension``). The journal cites
        # via its own ``[[ref:uuid]]`` mechanism (_reflect_claims). The gathered
        # [N] extension is NO LONGER dropped: V4 rewrites resolvable gathered [N]
        # markers to [[ref:uuid]] post-NARRATE so GATHER-found corpus docs are
        # journal-citable too. GATHER itself stays byte-for-byte.
        gathered_context, gather_usage, _gather_refs, _, gather_citation_extension = (
            await _gather(
                deps,
                binding=active_binding,
                user_prompt=user_prompt,
                target_id=None,
                analyst_id=analyst_id,
                steps=steps,
                tool_bindings=options.get("gather_tool_bindings") or {},
                gather_system=gather_system,
                extra_read_tools=JOURNAL_READ_TOOLS,
                extra_write_tools=JOURNAL_PROPOSE_TOOLS,
            )
        )
        _fold(gather_usage)
        if gathered_context:
            user_prompt = f"{gathered_context}\n{user_prompt}"
    else:
        steps.append({"phase": "gather", "kind": "no_binding"})

    # --- FIELD-NOTES seam (§4.3) — in-voice cited handoff, NOT a thin summary ---
    field_notes, fn_usage = await _field_notes(
        deps, base_prompt=user_prompt, analyst_id=analyst_id, steps=steps,
    )
    _fold(fn_usage)

    # --- NARRATE (§4.4) — write the entry, tools LIVE -------------------
    try:
        body, narrate_usage = await _narrate_with_tools(
            deps,
            field_notes=field_notes,
            binding=active_binding,
            analyst_id=analyst_id,
            steps=steps,
        )
    except Exception:
        raise
    _fold(narrate_usage)
    body = (body or "").strip()

    # --- E-1 lens/lens_diff EMPTY-READ fallback (2026-07-27 sweep + the P3
    # 2026-07-31 extension) --------------------------------------------------
    # A VOICES tier whose NARRATE returned nothing gets ONE more narrate pass
    # with an explicit per-tier redirect (lens: the verified tower corpus, the
    # material chronicle/consolidation reason over when the slice/acquisition
    # is dark; lens_diff: re-pull THIS cycle's lens reads + continuity — it
    # has no tower slice of its own to fall back into). Best-effort: any
    # failure inside the fallback degrades to the honest empty body below —
    # it never fails a run the primary narrate survived. A read still empty
    # after the fallback stays honestly "(empty lens read)" / "(empty chorus
    # diff)" and logs a WARNING (a healthy roster shipping an empty diff is
    # worth an operator's attention even though the run itself succeeds).
    _empty_fallback_instruction = _EMPTY_FALLBACK_INSTRUCTION_FOR.get(entry_kind)
    if _empty_fallback_instruction is not None and not body:
        _fallback_kind = f"empty_{entry_kind}_fallback"
        steps.append({"phase": "narrate", "kind": _fallback_kind})
        try:
            fb_body, fb_usage = await _narrate_with_tools(
                deps,
                field_notes=field_notes + _empty_fallback_instruction,
                binding=active_binding,
                analyst_id=analyst_id,
                steps=steps,
            )
            _fold(fb_usage)
            body = (fb_body or "").strip()
        except Exception as exc:  # degrade-not-drop — honest empty beats a crash
            logger.warning(
                "journal_assessor.empty_%s_fallback.failed id=%s err=%s",
                entry_kind, analyst_id, exc,
            )
        steps.append({
            "phase": "narrate",
            "kind": (
                f"{_fallback_kind}_recovered"
                if body else f"{_fallback_kind}_still_empty"
            ),
            "body_chars": len(body),
        })
        if not body:
            logger.warning(
                "journal_assessor.empty_%s_fallback.still_empty analyst_id=%s",
                entry_kind, analyst_id,
            )

    # --- Consolidation prose-shape guard (persist-time backstop) -----------
    # Live defect 2026-07-31 02:07Z (see the module docstring above
    # ``_is_consolidation_shape_rejected``). Scoped to the consolidation tier
    # — the tier the defect hit and the one whose write fires
    # ``supersede_prior_consolidation``, so a persisted garbage row is the
    # most expensive kind of wrong here. One retry with a hard prose-only
    # instruction; a retry that ALSO looks like an envelope leaves
    # ``consolidation_shape_rejected`` set, and the run publishes NOTHING —
    # an absent entry beats a garbage one — while still tracing full-fidelity
    # for audit (mirrors inline_target's D4 off-target guard: force
    # TRACE_ONLY rather than drop the run outright).
    consolidation_shape_rejected = False
    if is_consolidation and _is_consolidation_shape_rejected(body):
        logger.warning(
            "journal_assessor.consolidation_shape_rejected retry=1 "
            "body_chars=%d preview=%r",
            len(body), body[:200],
        )
        steps.append({
            "phase": "narrate", "kind": "consolidation_shape_rejected",
            "retry": 1, "body_chars": len(body),
        })
        retry_body = ""
        try:
            retry_body, retry_usage = await _narrate_with_tools(
                deps,
                field_notes=field_notes + _CONSOLIDATION_SHAPE_RETRY_INSTRUCTION,
                binding=active_binding,
                analyst_id=analyst_id,
                steps=steps,
            )
            _fold(retry_usage)
            retry_body = (retry_body or "").strip()
        except Exception as exc:  # degrade-not-drop — same idiom as the lens fallback
            logger.warning(
                "journal_assessor.consolidation_shape_rejected.retry_failed err=%s",
                exc,
            )
        if retry_body and not _is_consolidation_shape_rejected(retry_body):
            body = retry_body
            steps.append({
                "phase": "narrate", "kind": "consolidation_shape_recovered",
                "body_chars": len(body),
            })
        else:
            consolidation_shape_rejected = True
            logger.warning(
                "journal_assessor.consolidation_shape_rejected "
                "retry=1/fatal body_chars=%d preview=%r",
                len(retry_body), retry_body[:200],
            )
            steps.append({
                "phase": "narrate", "kind": "consolidation_shape_rejected_fatal",
                "body_chars": len(retry_body),
            })

    # --- V4 (GATHER [N]→[[ref:uuid]] bridge) — BEFORE reflect --------------
    # A [N] the narrator wrote against a GATHER-gathered corpus doc dies at render
    # (the journal renders [[ref:uuid]], never [N]). Rewrite the resolvable
    # gathered [N] markers to durable [[ref:uuid]] so REFLECT binds them as real
    # cited fact claims (J4). Unmapped ordinals + the native [[ref:uuid]] path are
    # untouched; full-width bracket variants are normalized first.
    body, _v4_rewritten = _rewrite_gathered_citations(body, gather_citation_extension)
    if _v4_rewritten:
        steps.append({
            "phase": "reflect",
            "kind": "gathered_citation_bridge",
            "rewritten": _v4_rewritten,
        })

    # --- REFLECT (§10) — permissive per-claim citation flag (flag, don't strip) ---
    claims, cited_refs, reflect_flags = _reflect_claims(body)
    steps.append({
        "phase": "reflect",
        "kind": "permissive_citation_flag",
        "claims": len(claims),
        "cited_refs": len(cited_refs),
        "flags": list(reflect_flags),
    })

    # --- PROPOSE (§7 Wave 4) — the human-gated write-back coda ---------------
    # Engaged iff the journal_propose pack is EFFECTIVE for this class (the SAME
    # ``gather_write_prompt_fragments`` signal the GATHER catalog gates on —
    # entry + consolidation today; chronicle/lens grant no propose pack) AND
    # there is an entry to reason over. See the ``_propose_phase`` block above
    # for why it lives here, not in GATHER/NARRATE. It can never fail the run.
    if write_fragments is not None and body and not consolidation_shape_rejected:
        _fold(
            await _propose_phase(
                deps,
                body=body,
                cited_refs=cited_refs,
                analyst_id=analyst_id,
                tool_bindings=options.get("gather_tool_bindings") or {},
                write_fragments=write_fragments,
                steps=steps,
            )
        )
    elif write_fragments is None:
        steps.append({"phase": "propose", "kind": "pack_not_effective"})

    # --- HONESTY (§10) — DETERMINISTIC honesty_flags forced from substrate ----
    honesty_flags = await _forced_honesty_flags(active_binding, steps=steps)
    honesty_flags += await _source_health_cross_check(
        active_binding, inputs, steps=steps
    )
    # S-1 (SWEEP_SYNTHESIS §T1-#1) — the prose-vs-instrument NUMBER guard: do the
    # collection-posture COUNTS the narrator WROTE match the live tool? Runs on
    # EVERY tier (the sibling cross_check reads the gather slice, so it no-ops on
    # the lens path where the fabrication was found). Flags, never rewrites.
    honesty_flags += await _source_health_number_check(
        active_binding, body, steps=steps
    )
    # V2.2 — DETERMINISTIC apparatus-lead flag (R-3 style honesty, not a block).
    # The persona forbids opening on the apparatus; when the narrator regresses
    # ("I start by checking the health of my senses…") ANNOTATE the entry.
    # Idempotent append (mirrors _stamp_journal_contradicted_flag's @> guard) —
    # never duplicate the flag if a substrate stamp above already added it.
    for _flag in _apparatus_lead_flag(body):
        if _flag not in honesty_flags:
            honesty_flags.append(_flag)
            steps.append({"phase": "honesty", "kind": "apparatus_lead"})

    # --- VOICES LV-1 data column (§2.9 / §3.3) — per-tier metadata -----------
    # lens → {"lens_id": <analyst_id>}; lens_diff → {"matrix": {roster}} computed
    # deterministically from get_lens_reads; every other tier → {} (unchanged).
    # prior_version is DELIBERATELY omitted from a lens row — the row's own
    # analyst_version column IS the prior version (DL-2); stance_faithfulness is a
    # VERIFY-TIME judgment stamped into a side critique row, not here.
    row_data: dict[str, Any] = {}
    if entry_kind == "lens":
        row_data = {"lens_id": analyst_id}
    elif entry_kind == "lens_diff":
        row_data = {"matrix": await _compute_lens_diff_matrix(
            active_binding, steps=steps
        )}

    title = _derive_title(
        body,
        fallback=_TITLE_FALLBACK_FOR.get(entry_kind, "Journal entry"),
    )
    # ``supersedes`` is NOT decided here — the write path closes the prior open
    # consolidation on the SAME conn immediately before the insert and records the
    # link via the prior row's superseded_by pointer (§8). We leave it None on the
    # payload (the bootstrap-safe default) for BOTH tiers; supersede_prior_
    # consolidation only fires when entry_kind == 'consolidation'.
    payload = JournalPayload(
        entry_kind=entry_kind,
        title=title,
        body=body or _EMPTY_BODY_FOR.get(entry_kind, "(empty entry)"),
        claims=claims,
        cited_substrate_refs=cited_refs,
        period_start=period_start,
        period_end=period_end,
        supersedes=None,
        honesty_flags=honesty_flags,
        data=row_data,
    )
    steps.append({
        "phase": "narrate",
        "kind": "coerce_journal",
        "entry_kind": entry_kind,
        "body_chars": len(body),
        "claims": len(claims),
        "cited_refs": len(cited_refs),
        "honesty_flags": list(honesty_flags),
        "data_keys": sorted(row_data.keys()),
    })

    # --- PERSIST -------------------------------------------------------
    # derived_from is EMPTY — the journal is the direction-asymmetric off-chain
    # node (§3.5). The citations live in claims / cited_substrate_refs only; the
    # write path (_insert_journal_entry) also hard-forces the column empty.
    steps.append({"phase": "persist", "kind": "envelope", "derived_from": 0})

    # KW-1 forward-consumption index (migration 0106): the journal's
    # consumption point is its RENDERED slice — ``_select_journal_slice`` is
    # what every tier's renderer actually put in front of the narrator (a
    # deterministic pure function of ``inputs``, so re-applying it here yields
    # exactly the set the render used). Stamped as ``consumed_edges``; the
    # runtime materializes them into ``output_consumption`` alongside the
    # journal row write (context='journal_slice'), best-effort. This is a
    # SIDECAR index, deliberately NOT ``derived_from`` — the journal stays the
    # off-chain node (§3.5); the forward index is how "this entry read row F"
    # survives without putting the journal on the lineage chain.
    consumed_edges: list[tuple[UUID, str]] = []
    for _row in _select_journal_slice(inputs):
        _rid = _row.get("id")
        if _rid is None:
            continue
        try:
            consumed_edges.append(
                (UUID(str(_rid)), CONSUMPTION_CONTEXT_JOURNAL)
            )
        except (ValueError, AttributeError, TypeError):
            continue  # malformed row id — the render tolerated it; so do we

    return AnalystMethodResult(
        finding=payload,            # the runtime forwards this to write_analyst_output(kind=JOURNAL)
        usage=usage,
        derived_from=[],            # OFF the chain (§3.5)
        intermediate_steps=steps,
        consumed_edges=consumed_edges,
        # consolidation prose-shape guard — a rejected-after-retry body still
        # traces (audit), but never publishes as a visible journal_entries row.
        force_trace_only=consolidation_shape_rejected,
    )


__all__ = [
    "KIND_NAME",
    "OUTPUT_KIND",
    "READ_SLICE",
    "run_method",
    "build_prompt_module",
    "CONSOLIDATOR_ANALYST_ID",
    "CONSOLIDATOR_PROMPT_MODULE_PATH",
    "CHRONICLE_ANALYST_ID",
    "CHRONICLE_PROMPT_MODULE_PATH",
    # VOICES LV-1 — the faculty-lens tier.
    "LENS_ANALYST_IDS",
    "LENS_PROMPT_MODULE_PATHS",
    "LENS_DIFF_ANALYST_ID",
    "LENS_DIFF_PROMPT_MODULE_PATH",
    # task #236 — the deterministic NARRATE tool-call-leak guard.
    "NarrateToolCallLeakError",
]
