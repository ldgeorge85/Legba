# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The DESK-GROUNDING backstops — what a claim resting ONLY on a block may say.

ONE FAMILY, TWO MEMBERS, ONE PREMISE. A DESK GROUNDING block (the prior read,
the window ledger, the open-situation register) is deliberately CITABLE: an
UNCITED prior leaking into cited analysis is this platform's named failure
mode, so grounding context enters as numbered, gradeable evidence like
anything else. Being citable is what makes a block-backed clause resolve —
and it is also what EXEMPTS such a clause from the standing "no claim may
rest on orientation alone" rule, because the clause does not rest on
orientation alone any more: it rests on a ref. That exemption is the bug both
guards here close, from the verify side, on two different things a block
cannot warrant:

  * **H1 — SELF-CORROBORATION.** The register says a frame is intense and
    recent. The desks wrote that intensity themselves, out of their own "no
    material change" bookkeeping. A claim that the world is still doing
    something, warranted ONLY by the register, is warranted by nothing.
  * **Q4 — THE CARRIED DATED ANCHOR.** The prior read and the window ledger
    say what this desk SAID. A DATE or a FIGURE inside one of them is last
    cycle's claim, not this cycle's reporting, so a dated or numeric claim
    warranted ONLY by memory is a fact that has stopped being checked — and
    it gets more confident every cycle it survives.

Both are SOFT and both are INERT unless the finding actually cites the block
in question, so the overwhelming majority of the fleet's verdicts are
untouched and byte-identical.

WHY IT IS ITS OWN MODULE (2026-09-28). ``verify.py`` stood at 5,900 lines
against its 5,900 DO-NOT-RAISE ceiling, and the gate is honoured by SPLITTING,
never by pinning a new number. H1 was the cohesive unit to take: a banner, four
constants and one function that only ever talk to each other, and Q4 is the
second member of exactly that family rather than a new one. ``verify``
RE-EXPORTS every name moved here, so ``verify.register_self_corroboration_spans``
and ``verify._REGISTER_REF_KIND`` resolve exactly as before.

WHAT STAYS IN ``verify.py`` — the FOLDS (``_fold_guard_spans``) and the fail
class table, for the reason the ``world_knowledge_guards`` banner already
recorded: they manipulate the report + ledger types that module owns, and the
fold is shared with W31, the absence route and the M13/M15 family.

THE IMPORT EDGE. ``verify`` imports THIS module, so every ``verify`` helper
these guards need — ``UnsupportedSpan``, ``_segment_claims``,
``_is_fact_asserting``, ``_markers_in_claim``, ``_build_ordinal_map`` —
late-binds through :func:`_verify` at call time (the ``judge_input_checks`` /
``world_knowledge_guards`` pattern). ``data.provenance`` must not import
``data.analysts`` at all, so the two analyst-side tokens these guards key on
(the register's ``ref_kind`` and the licensed carry disclosure) are RESTATED
as literals here and held against their owners by drift-guard tests, exactly
as ``assessment_weighting`` restates three whole lexicons.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover — annotations only
    from .verify import UnsupportedSpan

__all__ = [
    "CARRIED_UNSUPPORTED",
    "REGISTER_SELF_CORROBORATION",
    "carried_anchor_spans",
    "memory_only_citations",
    "register_self_corroboration_spans",
]


def _verify():
    """Lazy accessor — this module is imported BY verify, so the edge runs one
    way at call time only (the ``world_knowledge_guards`` pattern)."""
    from . import verify

    return verify


# ---------------------------------------------------------------------------
# H1 — SELF-CORROBORATION FROM THE REGISTER (CORRECTNESS-R2 M-1 / rec. 1)
# ---------------------------------------------------------------------------
#
# THE DEFECT, verbatim from the round's worst read. The AR escalation head:
# "The open-situation register records a high intensity (59.1) and recent
# activity, indicating concrete operational impact rather than mere rhetoric" —
# confidence 0.90, citing ordinals past its own slice count, i.e. the register
# grounding block. The AR composition's BLUF: "the register CONFIRMS the strike
# remains the active driver with high intensity". The strike had ended three
# weeks earlier; the register's intensity was the sum of the desks' own "no
# material change" bookkeeping.
#
# The register is ``[[ref:N]]``-citable — deliberately, because an UNCITED prior
# leaking into cited analysis is this platform's named failure mode. But being
# citable is exactly what exempts it from the standing "no claim may rest on
# orientation alone" clause, and that exemption is the bug. This guard closes it
# from the verify side: a world claim whose ONLY support is the register, stated
# in the language of currency or corroboration, is not supported by anything.
#
# SOFT, and the class is earned rather than assumed. The clause is not
# fabricating a citation (the register block is real and the model was shown it)
# and its cited source does not contradict it — it claims a VERIFICATION STATUS
# its evidence cannot carry, which is the ``_UNHEDGED_PERIPHERY`` /
# ``hedge_laundering`` overclaim family. The render-side rule
# (``window_ledger.REGISTER_SELF_CORROBORATION_RULE``) is the primary fix; this
# is the backstop that measures whether it took.
#
# CONSERVATIVE ON BOTH LEGS, because a false positive here demotes a legitimate
# continuity sentence:
#   * the claim's resolved markers must ALL be register ordinals — one basis or
#     ledger citation alongside and the clause has real evidence, so it passes;
#   * and it must assert CURRENCY ("remains", "ongoing", "continues") or
#     CORROBORATION ("confirms", "corroborates"). A clause that merely reports
#     what the register holds ("the register lists three open frames") asserts
#     nothing about the world and is never flagged.

REGISTER_SELF_CORROBORATION = "register_self_corroboration"

#: ``ref_kind`` the register citation carries
#: (``window_ledger.SITUATION_REGISTER_REF_KIND``). Duplicated as a literal
#: rather than imported: ``data.provenance`` sits UNDER ``data.analysts`` in the
#: import graph, and the analysts package imports verify. A drift-guard test
#: asserts the two strings are equal.
_REGISTER_REF_KIND = "situation_register"

#: CORROBORATION language — the product vouching for the world with its own
#: bookkeeping. These are the verbs that turn an orienting index into evidence.
_REGISTER_CORROBORATION_RE = re.compile(
    r"\b(?:confirm(?:s|ed|ing)?|corroborat(?:es|ed|ing|ion)"
    r"|substantiat(?:es|ed|ing)|validat(?:es|ed|ing)|verif(?:ies|ied|ying)"
    r"|bears?\s+out|attests?\s+to|demonstrat(?:es|ed|ing))\b"
)

#: CURRENCY language — asserting that the underlying event is still happening.
#: The exact shapes the AR read used ("remains the active driver", "the strike
#: continues", "ongoing"), plus the near neighbours.
_REGISTER_CURRENCY_RE = re.compile(
    r"\b(?:remains?|remaining|continu(?:es|ing)|ongoing|still\s+\w+"
    r"|persist(?:s|ing)|sustain(?:s|ing|ed)|active\s+driver"
    r"|currently\s+(?:active|underway|ongoing)|underway"
    r"|shows?\s+no\s+sign\s+of\s+(?:easing|abating))\b"
)


def _ordinal_ref_kinds(citations: Any) -> dict[int, Any]:
    """``{ordinal -> ref_kind}`` through THE ONE ordinal builder (C-4).

    Routed through the shared builder rather than hand-rolled, and wrapped by
    its caller in a ``try`` — a guard never breaks the verify path.
    """
    return _verify()._build_ordinal_map(
        citations, lambda entry, _n: entry.get("ref_kind")
    )


def register_self_corroboration_spans(
    body: str, citations: Any
) -> list[UnsupportedSpan]:
    """FLAG world claims resting ONLY on the open-situation register (H1).

    One soft :data:`REGISTER_SELF_CORROBORATION` span per hit, text = the RAW
    segmented claim (the #116c judge-path dedup matches on it, so a claim the
    judge already graded stays the judge's).

    INERT unless the finding actually cites the register: with no
    ``ref_kind='situation_register'`` citation in the list this returns ``[]``
    without segmenting the body, so every finding in the fleet that never sees a
    register block is byte-identical to its pre-H1 verdict.

    Never raises — a malformed citation list yields no spans, never an exception
    inside the verify path.
    """
    if not body:
        return []
    v = _verify()
    try:
        register_ords = {
            n
            for n, kind in _ordinal_ref_kinds(citations).items()
            if kind == _REGISTER_REF_KIND
        }
    except Exception:  # noqa: BLE001 — a guard never breaks the verify path
        return []
    if not register_ords:
        return []
    spans: list[UnsupportedSpan] = []
    for claim in v._segment_claims(body):
        if not v._is_fact_asserting(claim):
            continue
        markers = v._markers_in_claim(claim, subclaim=True) or v._markers_in_claim(
            claim, subclaim=False
        )
        # ALL resolved markers must be the register. A clause with one real
        # evidence citation beside it is grounded in that citation.
        if not markers or not all(n in register_ords for n in markers):
            continue
        low = claim.lower()
        if _REGISTER_CORROBORATION_RE.search(low) or _REGISTER_CURRENCY_RE.search(
            low
        ):
            spans.append(
                v.UnsupportedSpan(
                    text=claim,
                    reason=REGISTER_SELF_CORROBORATION,
                    markers=list(markers),
                )
            )
    return spans


# ---------------------------------------------------------------------------
# Q4 — THE CARRIED DATED ANCHOR (consult review 2026-09-28 §A / §B.8)
# ---------------------------------------------------------------------------
#
# THE DEFECT, and it survived three months of a working verify gate.
# ``leadership_transition`` on ``country_g20_fr`` (finding aa4a8991,
# 2026-09-27 19:37, confidence 0.9, 120 signals): *"the presidential hand-over
# from Emmanuel Macron to his successor is virtually certain on 28 September
# 2026"*. NO signal in that slice names the date or the event — the slice held
# Senate elections, a remark about a term-limited departure, and the Pope.
#
# WHY EVERY GATE PASSED IT. The date WAS in the rendered evidence: it sat in
# the PRIOR READ block, which is a citable ref carrying real
# ``evidence_text``, so the faithfulness judge — told to decide whether a
# claim "FOLLOWS FROM the evidence those markers name" — correctly answered
# yes, twice, at 1.00 (5/5 supported). The WINDOW LEDGER then re-fed the same
# sentence every cycle (24 findings since 07-01: "No material change: the
# scheduled 28 September handover remains"), the countdown ticking and the
# confidence rising, and the standing auditor searched six claims and decided
# none of them.
#
# THE RULE THIS ENCODES. A carried block is MEMORY, not evidence, for any
# DATED or NUMERIC claim: memory blocks RE-STATE, only SIGNAL blocks
# RE-ESTABLISH. A date or figure that appears only in a prior-read or
# window-ledger ref, and in no cited signal block, is unsupported — and it
# counts against the faithfulness fold exactly like an uncited prose fact,
# because that is what it is.
#
# THE ONE EXCEPTION IS THE BLOCK'S OWN STAMP, and it has to be, because the
# house's licensed continuity idiom is built on it: "no material change since
# the 3 August morning read [N]" and "mass protests recorded on 7 August [N]
# have since subsided" are claims about THE RECORD, which the block genuinely
# does establish. So the exemption is:
#
#   * PRIOR READ — its ``produced_at``, read off the STRUCTURED citation field
#     (``unit_grounding.citation_for_block`` stamps it; the composition's
#     ``_build_composition_citation`` stamps it too). NEVER off the block's
#     rendered text, because that text CONTAINS the previous read's prose —
#     which is precisely where the France date lived. Reading the body would
#     re-open the hole this guard exists to close.
#   * WINDOW LEDGER — its whole ``evidence_text``, because the ledger block is
#     an INDEX and not prose: a header plus one dated line per read, with no
#     body excerpt anywhere in it. Every date in it is a date this record
#     stamps. KNOWN AND ACCEPTED HOLE: a ledger line's TITLE is a unit's own
#     one-line claim, so a title that itself carries a date would exempt that
#     date. That is the conservative direction — a miss, not a false flag —
#     and it is the cheap error here, exactly as it is for H1 and W31.
#
# CONSERVATIVE ON FOUR LEGS, because a false positive demotes a legitimate
# continuity sentence and poisons a faithfulness score:
#   * INERT unless the finding actually cites a memory block;
#   * the claim's resolved markers must ALL be memory ordinals — one signal
#     citation alongside and the sentence has real evidence, so it passes
#     (this guard never second-guesses what a cited signal says; the judge
#     does that);
#   * the claim must carry a DATE or a NUMERAL that is not one of the cited
#     blocks' own stamps — a purely qualitative carry ("the security picture
#     is unchanged since the prior read [7]") asserts no anchor and is never
#     flagged;
#   * and a claim that DISCLOSES the carry in the licensed words — "carried
#     from the prior read, not re-sourced this window" — is never flagged,
#     because that sentence is the honest move the prompt asks for and
#     flagging it would make the rule unfollowable.
#
# WHY THIS GUARD DOES *NOT* TAKE H1'S ``_is_fact_asserting`` GATE, and the
# answer is the live bytes. The sentence that carried the France anchor
# forward, verbatim from head aa4a8991:
#
#     "- No material change: the constitutionally scheduled hand-over on
#      28 September 2026 remains unchanged (see prior read [120] and the
#      window-ledger entry [121])."
#
# ``_is_fact_asserting`` returns FALSE on it — it is a no-change / absence
# idiom, and the floor EXEMPTS that class deliberately (the #1 honest-low-risk
# calibration). That exemption is why the sentence was never a checkable claim
# on the floor and why the whole carry was free. A Q4 gated on fact-assertion
# would therefore be blind to precisely the shape it exists to catch, and
# would have scored this very head clean. So the gate is the CITATION
# instead: a span with no marker is the floor's ``no_citation`` business, and
# a span whose markers all resolve to memory is this guard's whatever mood the
# sentence is in. W31 reached the same place from the other direction — it
# runs ONLY on floor-exempt spans, for the same reason.
#
# THE ARITHMETIC, stated because it differs by path. A floor-EXEMPT claim (the
# live case) folds in as ONE new checkable-but-unsupported claim — the W31
# shape exactly. A claim the floor already counted SUPPORTED (a grounding-
# backed citation resolves on the floor, by QW1-B design) gains a failed row
# beside its supported one, so the denominator rises and the score falls: the
# same arithmetic H1 has had since August, and the direction the lane wants.

CARRIED_UNSUPPORTED = "carried_unsupported"

#: The ``ref_kind`` values that mean MEMORY — this desk's own previous output,
#: re-rendered. Both are members of ``kinds.GROUNDING_REF_KINDS``; what
#: separates them from the other five is that their content is ANALYST PROSE
#: this pipeline wrote, not a record of the world.
#:
#: Restated as literals rather than imported for the module-banner reason
#: (``data.provenance`` may not import ``data.analysts``); a drift-guard test
#: pins them against ``window_ledger.CONTINUITY_PRIOR`` /
#: ``WINDOW_LEDGER_REF_KIND`` and ``unit_grounding.GROUNDING_PRIOR_READ``.
_MEMORY_REF_KINDS: frozenset[str] = frozenset({"prior_read", "window_ledger"})

#: THE COMPOSITION'S SECOND SPELLING. A unit's prior-read citation carries
#: ``ref_kind='prior_read'``; a COMPOSITION's carries ``ref_kind='finding'``
#: (its drill target really is a finding row) and says "this is the prior
#: read" on ``continuity`` instead — ``composition_citations`` sets
#: ``CONTINUITY_CITATION_KEY`` to ``CONTINUITY_PRIOR`` there. Reading only
#: ``ref_kind`` would leave the composition tier unguarded, which is the tier
#: the France claim was carried UP through (country_composition 8c54f65c).
_CONTINUITY_KEY = "continuity"

#: The LICENSED DISCLOSURE, restated from ``_tradecraft.CARRIED_ANCHOR_DISCLOSURE``
#: and pinned to it by a drift-guard test. The short token deliberately matches
#: every spelling the prompt offers: "carried from the prior read, not
#: re-sourced this window", "carried from the window ledger, not re-sourced
#: this window", and the block note's bare "not re-sourced".
_CARRY_DISCLOSURE = "not re-sourced"

#: Month names, for the calendar-date shapes a unit is INSTRUCTED to write
#: (``_tradecraft._DATED_CLAIM_RULE``: "a plain calendar date — 'on 2 August',
#: 'reported 31 July'"). Abbreviations included because a model writes them
#: even where the rule asks for the full form.
_MONTHS: tuple[str, ...] = (
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
)
_MONTH_ABBR: dict[str, int] = {}
for _i, _m in enumerate(_MONTHS, start=1):
    _MONTH_ABBR[_m] = _i
    _MONTH_ABBR[_m[:3]] = _i
_MONTH_ALT = "|".join(sorted(_MONTH_ABBR, key=len, reverse=True))

#: ``28 September 2026`` / ``28 September`` / ``3 Aug``.
_DAY_MONTH_RE = re.compile(
    rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_ALT})\b\.?(?:\s+(\d{{4}}))?",
    re.IGNORECASE,
)
#: ``September 28, 2026`` / ``September 28``.
_MONTH_DAY_RE = re.compile(
    rf"\b({_MONTH_ALT})\b\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?",
    re.IGNORECASE,
)
#: ``2026-09-28`` — banned in prose by ``NO_INSTRUMENT_READINGS``, read here
#: anyway because the STAMP side (a ``produced_at``, a ledger ``read_date``) is
#: written in exactly this form.
_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")

#: A run of digits (with internal separators) left after the markers and the
#: dates are masked out — the FIGURE leg's unit of comparison.
_NUMERAL_RUN_RE = re.compile(r"\d[\d,.]*")

#: Citation markers, in every spelling a stored body can carry: the unit
#: ``[7]``, the composition ``[[ref:7]]``, the defused ``[prior:7]`` a prior
#: read's embedded body is rewritten to, and the licensed prose spelling
#: ``(prior read ref 7)``. All are masked before a single digit is read —
#: an ordinal is a handle, never a figure about the world.
_MARKER_MASK_RE = re.compile(
    r"\[\[ref:\s*\d+\s*\]\]"
    r"|\[\s*prior\s*:\s*\d+\s*\]"
    r"|\[\s*\d+(?:\s*[-–]\s*\d+)?\s*\]"
    r"|[(\[]\s*prior[\s_\-]*read\s*(?:ref(?:erence)?)?\s*[:\s]\s*\d+\s*[)\]]",
    re.IGNORECASE,
)


def _mask_markers(text: str) -> str:
    """``text`` with every citation marker blanked, LENGTH PRESERVED."""
    return _MARKER_MASK_RE.sub(lambda m: " " * (m.end() - m.start()), text)


def _dates_in(text: str) -> set[tuple[int, int]]:
    """The ``(month, day)`` pairs a text states, in any of the three shapes.

    The YEAR is deliberately DROPPED from the key. A prior read produced on
    2026-09-27 and a claim saying "27 September" are the same stamp, and the
    claim is not required to print the year — while a claim that prints a
    DIFFERENT year on the same day-month is vanishingly rare next to the
    false-flag cost of missing the stamp. Conservative direction, stated.
    """
    out: set[tuple[int, int]] = set()
    for m in _DAY_MONTH_RE.finditer(text):
        month = _MONTH_ABBR.get(m.group(2).lower())
        if month:
            out.add((month, int(m.group(1))))
    for m in _MONTH_DAY_RE.finditer(text):
        month = _MONTH_ABBR.get(m.group(1).lower())
        if month:
            out.add((month, int(m.group(2))))
    for m in _ISO_DATE_RE.finditer(text):
        out.add((int(m.group(2)), int(m.group(3))))
    return out


def _strip_dates(text: str) -> str:
    """``text`` with every recognised date blanked, LENGTH PRESERVED — so what
    is left for the NUMERAL test is a figure and never a date's own digits."""
    for pattern in (_DAY_MONTH_RE, _MONTH_DAY_RE, _ISO_DATE_RE):
        text = pattern.sub(lambda m: " " * (m.end() - m.start()), text)
    return text


def _memory_stamps(citations: Any) -> tuple[set[int], set[tuple[int, int]], str]:
    """``(memory ordinals, their stamp dates, the ledger's stamp text)``.

    The two halves of "what a memory block ESTABLISHES about itself":

    * a PRIOR READ establishes WHEN IT WAS PRODUCED, and nothing else. Its
      ``produced_at`` is read off the STRUCTURED citation field — never off
      ``evidence_text``, which carries the previous read's own prose and is
      exactly where a carried anchor hides. It contributes a DATE and no
      numerals: its ISO stamp is full of two-digit runs that would exempt half
      the numbers in the language.
    * a WINDOW LEDGER establishes every date on its own lines, plus the window
      length and line count its header prints. The whole block is an INDEX
      with no prose body, so its ``evidence_text`` IS its stamp, and it
      contributes both dates and numerals.
    """
    v = _verify()

    def _project(entry: Any, n: int) -> Any:
        kind = entry.get("ref_kind")
        continuity = entry.get(_CONTINUITY_KEY)
        is_prior = kind == "prior_read" or continuity == "prior_read"
        is_ledger = kind == "window_ledger" or continuity == "window_ledger"
        if not (is_prior or is_ledger):
            return v._ORDINAL_SKIP
        return (entry, is_prior, is_ledger)

    ordinals: set[int] = set()
    dates: set[tuple[int, int]] = set()
    ledger_text: list[str] = []
    for n, (entry, is_prior, is_ledger) in v._build_ordinal_map(
        citations, _project
    ).items():
        ordinals.add(n)
        if is_ledger:
            text = entry.get("evidence_text")
            if isinstance(text, str):
                ledger_text.append(text)
                dates |= _dates_in(text)
        if is_prior:
            produced = entry.get("produced_at")
            if isinstance(produced, str):
                dates |= _dates_in(produced)
    return ordinals, dates, "\n".join(ledger_text)


def _span(v: Any, claim: str, markers: list[int]) -> UnsupportedSpan:
    """ONE soft carried-anchor span, built through verify's own type."""
    return v.UnsupportedSpan(
        text=claim, reason=CARRIED_UNSUPPORTED, markers=list(markers)
    )


def carried_anchor_spans(body: str, citations: Any) -> list[UnsupportedSpan]:
    """FLAG dated/numeric claims resting ONLY on MEMORY blocks (Q4).

    One soft :data:`CARRIED_UNSUPPORTED` span per hit, text = the RAW segmented
    claim, so the #116c judge-path dedup keeps the judge authoritative over
    prose it graded — the same posture H1 and W31 take, and the reason this is
    folded BEFORE the judge.

    INERT unless the finding actually cites a prior read or a window ledger:
    with no memory citation in the list this returns ``[]`` without segmenting
    the body, so every finding that never sees one is byte-identical to its
    pre-Q4 verdict.

    Never raises — a malformed citation list yields no spans, never an
    exception inside the verify path.
    """
    if not body:
        return []
    v = _verify()
    try:
        memory_ords, stamp_dates, ledger_text = _memory_stamps(citations)
    except Exception:  # noqa: BLE001 — a guard never breaks the verify path
        return []
    if not memory_ords:
        return []
    spans: list[UnsupportedSpan] = []
    for claim in v._segment_claims(body):
        markers = v._markers_in_claim(claim, subclaim=True) or v._markers_in_claim(
            claim, subclaim=False
        )
        # ALL resolved markers must be memory. One signal citation alongside and
        # the sentence rests on reporting — whether that reporting says what the
        # sentence says is the JUDGE's question, not this guard's. A span with
        # no marker at all is the FLOOR's business (``no_citation``), not this
        # guard's: Q4 is about what a citation WARRANTS, never about its
        # absence.
        if not markers or not all(n in memory_ords for n in markers):
            continue
        # The licensed honest move, never penalised: flagging the sentence the
        # prompt asks for would make the rule unfollowable.
        if _CARRY_DISCLOSURE in claim.lower():
            continue
        masked = _mask_markers(claim)
        if _dates_in(masked) - stamp_dates:
            spans.append(_span(v, claim, markers))
            continue
        # No un-stamped DATE. The FIGURE leg runs over what is left once the
        # markers AND every recognised date are blanked, so a date's own digits
        # can never be read as a figure. A numeral the ledger itself prints
        # (its window length, its line count, a line's own date) is the block's
        # stamp and is exempt; substring containment is the conservative
        # direction here — it can only ever EXEMPT, never flag.
        residue = _strip_dates(masked)
        figures = {
            m.group(0).strip(".,") for m in _NUMERAL_RUN_RE.finditer(residue)
        }
        if any(fig and fig not in ledger_text for fig in figures):
            spans.append(_span(v, claim, markers))
    return spans


# ---------------------------------------------------------------------------
# SW-9 (2026-10-03/1) — THE MEMORY-ONLY FINDING, at FINDING grain.
#
# Q4 above is the CLAIM-grain half: a dated or numeric sentence resting only on
# memory soft-fails. Its undated sibling — "No material change; the pressure
# stays moderate as recorded in the prior read [16] and the window ledger
# [17]" — is not a defect at claim grain at all: the judge is HANDED the prior
# read's text (QW1-B), the carry agrees with it, and "supported" is the correct
# answer to the question it was asked. The defect is the question. A unit
# finding whose EVERY resolved citation is memory has been checked against
# nothing but what this desk already said, so its tally is a consistency check,
# not a faithfulness measurement — and publishing it as one is how a carried
# invention scored 1.0 for weeks (live specimens 809fdee1 / bc997fb0, both
# 1.0, both citing only a prior read and/or a window ledger).
#
# The verdict here is consumed by ``resolve_score_state`` and becomes the
# ``carried_ungraded`` SCORE STATE — not a span, not a reason, nothing the
# tally sees. UNIT CONVENTION ONLY: a composition's citations are sub-claim
# ``[[ref:N]]`` refs whose prior read is ONE input among the desk reads it
# composes, and the region/world bytes are pinned by the voice-4 flip kit.
# ---------------------------------------------------------------------------


def memory_only_citations(body: str, citations: Any, indicators: Any = None) -> bool:
    """True iff a UNIT finding cites resolvable evidence and ALL of it is memory.

    "Cites" = every ``[N]`` / range marker in the body plus every structured
    indicator's citation index. "Resolvable" = an ordinal that reaches the
    judge's evidence map (a signal-backed entry or a grounding block with real
    captured text). A marker resolving to nothing counts neither way. One real
    signal citation anywhere — prose or indicator — and the answer is False.

    Never raises: a malformed list is False (the ordinary scored path).
    """
    if not body:
        return False
    v = _verify()
    try:
        if v._uses_subclaim_convention(citations):
            return False
        memory_ords, _dates, _ledger = _memory_stamps(citations)
        if not memory_ords:
            return False
        resolvable = v._signal_backed_ordinals(citations) | set(
            v._grounding_ordinals(citations)
        )
        cited = set(v._markers_in_claim(body, subclaim=False))
        if isinstance(indicators, (list, tuple)):
            for entry in indicators:
                if isinstance(entry, Mapping):
                    cited.update(v._indicator_citation_markers(entry))
    except Exception:  # noqa: BLE001 — a guard never breaks the verify path
        return False
    resolved = cited & resolvable
    return bool(resolved) and resolved <= memory_ords
