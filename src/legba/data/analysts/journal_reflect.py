# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""T1.0 (JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09 §6) — the REFLECT claim
machinery, extracted verbatim from ``journal_assessor.py`` when the module
pressed against its 3,000-line size-gate ceiling with three follow-on lanes
(T1.1 / T1.2 / T1.4) about to ADD to both the REFLECT pass and slice
selection. Behavior is byte-identical to the pre-extraction code — same
regexes, same branches, same docstrings — and ``journal_assessor`` re-exports
every name, so every existing import path (direct or via the ``journal_assessor``
module attribute, e.g. ``ja._reflect_claims``) keeps working unchanged.

Moved: ``_reflect_claims`` and everything ONLY it uses — the ``[[ref:<uuid>]]``
scanning regex, the speculation/instrument markers, the fact-vs-perspective
heuristics (the factual-hint regex, the first-person wonder/inference cue
regex, the classifier), and the Review-A ``[[instrument]]``-worldliness guard.
``JournalClaim`` itself lives in ``..provenance.models`` and is only imported
here, not defined here.

Left behind in ``journal_assessor.py``: the title-line regexes (``_TITLE_LINE_RE``
/ ``_BOLD_TITLE_RE`` / ``_MAX_TITLE_CHARS``) — used by ``_derive_title``, not by
REFLECT — and ``_select_journal_slice`` (a sibling lane, T2.2, extracts that one
to ``journal_slice.py`` next).
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping
from uuid import UUID

from ..provenance.models import JournalClaim

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
# B1 (T1.2, JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09 §3 P4 / §6) — the
# deterministic instrument-as-report honesty flag. A fact span whose refs are
# majority EVENT-CODED instrument rows (``journal_slice._is_instrument_row``,
# surfaced here via the caller's ``ref_labels`` map — REFLECT has no DB access
# of its own) and that carries NO ``[[instrument]]`` marker reads the coder's
# raw output as if it were a report. Flag-never-strip: this ANNOTATES the
# entry (mirrors ``journal_assessor._apparatus_lead_flag``'s contract); it
# never rewrites or drops the span.
# ---------------------------------------------------------------------------

INSTRUMENT_AS_REPORT_FLAG = "instrument_as_report"
_INSTRUMENT_SHARE_THRESHOLD = 0.5


def _flag_instrument_as_report(
    claims: list[JournalClaim], ref_labels: Mapping[str, str],
) -> list[str]:
    """Return ``[INSTRUMENT_AS_REPORT_FLAG]`` iff ANY cited fact claim's refs
    are >=50% rows the slice labelled ``'instrument'`` AND the claim's own
    text carries no ``[[instrument]]`` marker — else ``[]``.

    ``ref_labels`` maps a ref's string id to its ``journal_label``: only rows
    the slice actually labelled need appear (an unlabelled/unresolved ref is
    simply absent, counted as non-instrument, never crashes the lookup). An
    empty map degrades to no flag, never a false positive."""
    if not ref_labels:
        return []
    for claim in claims:
        if claim.kind != "fact" or not claim.refs:
            continue
        if _INSTRUMENT_MARKER_RE.search(claim.text_span):
            continue
        labels = [ref_labels.get(str(r)) for r in claim.refs]
        n_instrument = sum(1 for lb in labels if lb == "instrument")
        if n_instrument / len(claim.refs) >= _INSTRUMENT_SHARE_THRESHOLD:
            return [INSTRUMENT_AS_REPORT_FLAG]
    return []


# ---------------------------------------------------------------------------
# B2 (T1.4, JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09 §3 P6 / §6) — the
# deterministic routine-as-signal honesty flag. A fact span citing a
# scheduled-product row (``journal_slice._is_routine_row``, surfaced here via
# the caller's ``ref_labels`` map) that ALSO carries a change/causal
# connective reads a routine number as a signal of change without ever
# saying what it changed FROM. Flag-never-strip — ANNOTATES only.
# ---------------------------------------------------------------------------

ROUTINE_AS_SIGNAL_FLAG = "routine_as_signal"
# Kept identical to scripts/journal_connective_census.py's own
# _ROUTINE_CHANGE_CUES list, independently maintained (the census is a
# measurement tool; this is the live flag).
_ROUTINE_CHANGE_CUE_RE = re.compile(
    r"\b(?:compounds|stress|indicator|driven by|amid)\b", re.IGNORECASE
)


def _flag_routine_as_signal(
    claims: list[JournalClaim], ref_labels: Mapping[str, str],
) -> list[str]:
    """Return ``[ROUTINE_AS_SIGNAL_FLAG]`` iff ANY cited fact claim cites at
    least one ``'routine'``-labelled ref AND its own text carries a change/
    causal connective cue (compounds, stress, indicator, driven by, amid) —
    else ``[]``. Unlike the instrument flag this is NOT share-gated: a
    single routine ref dressed up as a driver of change is the whole
    defect — the baseline is either named in the sentence or it isn't."""
    if not ref_labels:
        return []
    for claim in claims:
        if claim.kind != "fact" or not claim.refs:
            continue
        if not any(ref_labels.get(str(r)) == "routine" for r in claim.refs):
            continue
        if _ROUTINE_CHANGE_CUE_RE.search(claim.text_span):
            return [ROUTINE_AS_SIGNAL_FLAG]
    return []


# ---------------------------------------------------------------------------
# B5 (JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09 §6, the T1.3 reader lane's
# live finding on entry dde520d3) — REPAIR a hand-copied ``[[ref:<uuid>]]``
# typo. The writer copies UUIDs from its gathered window by hand and
# occasionally transcribes one hex digit wrong (the police-civilian span's
# ref ``233bd806-…-4c3c-…`` — the real GDELT row is ``…-4e3c-…``, one digit
# off; the census independently saw a second near-miss). REPAIR a marker
# whose UUID matches NO id in the run's own gathered window but is within
# Hamming distance <= 2 of EXACTLY ONE candidate there; ambiguous (a tie) or
# too-far leaves it untouched — the reader renders an unresolved ref
# honestly rather than this guessing wrong. Flag-never-strip: the ref is
# NEVER dropped, only (sometimes) corrected in place.
#
# Runs BEFORE ``_reflect_claims`` on the raw body string, so ``_reflect_claims``
# itself is completely untouched — its byte-identity proof (T1.0) keeps
# holding for every existing caller that doesn't pass a ref set.
# ---------------------------------------------------------------------------

REF_REPAIRED_FLAG = "ref_repaired"
_HAMMING_MAX_DISTANCE = 2
_HEX_CHARS = frozenset("0123456789abcdef")


def _hex32(value: str) -> str:
    """Normalize a UUID-shaped string to its 32 lowercase hex chars (dashes
    stripped) — the comparison unit for the Hamming distance."""
    return value.replace("-", "").lower()


def _hamming(a: str, b: str) -> int:
    if len(a) != len(b):
        return max(len(a), len(b))  # both are validated 32-char hex; defensive only
    return sum(1 for x, y in zip(a, b) if x != y)


def _repair_ref_markers(
    body: str, gathered_ref_ids: Iterable[str] | None = None,
) -> tuple[str, list[dict[str, str]]]:
    """Repair a ``[[ref:<uuid>]]`` marker whose UUID isn't in
    ``gathered_ref_ids`` (the window rows the narrator was shown this run)
    but IS a unique Hamming-distance-<=2 match to one that is.

    ``gathered_ref_ids`` is ADDITIVE and OPTIONAL: ``None`` (the default —
    every existing caller) makes this a strict no-op, byte-identical to
    pre-B5 behavior. Returns ``(possibly-rewritten body, repairs)`` where
    each repair is ``{"from": <original marker uuid string>, "to": <repaired
    uuid string>}``, in the order the markers appear. A ref that already
    matches exactly, or has zero/multiple equally-close candidates, is left
    completely untouched (never guessed, never dropped)."""
    if not gathered_ref_ids:
        return body, []
    candidates: set[str] = set()
    for cand in gathered_ref_ids:
        h = _hex32(str(cand))
        if len(h) == 32 and set(h) <= _HEX_CHARS:
            candidates.add(h)
    if not candidates:
        return body, []

    repairs: list[dict[str, str]] = []

    def _sub(m: re.Match[str]) -> str:
        raw = m.group(1)
        norm = _hex32(raw)
        if norm in candidates:
            return m.group(0)  # exact match — untouched
        best_dist = _HAMMING_MAX_DISTANCE + 1
        best: list[str] = []
        for cand in candidates:
            d = _hamming(norm, cand)
            if d < best_dist:
                best_dist, best = d, [cand]
            elif d == best_dist:
                best.append(cand)
        if best_dist > _HAMMING_MAX_DISTANCE or len(best) != 1:
            return m.group(0)  # too far, or ambiguous — leave it as-is
        repaired_uuid = str(UUID(best[0]))
        repairs.append({"from": raw, "to": repaired_uuid})
        return f"[[ref:{repaired_uuid}]]"

    new_body = _REF_MARKER_RE.sub(_sub, body)
    return new_body, repairs
