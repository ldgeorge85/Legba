# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G3 — THE WEIGHTED-COMPARISON LICENCE: the record's own ranking, relayed.

THE DEFECT, and it is a GRADER defect rather than a fabrication. D-6's N=5
clause replay (``planning/D6_PROMPT_CLAUSE_REPORT.md`` §4.4) measured the
instrument-prose class falling 7 of 11 judge failures to 3 of 8 — the prompt
clause did its job — and watched the failures REDISTRIBUTE rather than
disappear. The new leading residual (1 → 3) is one shape:

    "Together they push the day's overall risk profile upward, with no single
     driver eclipsing the others [[ref:1]]."

    "...though it treats this as a secondary factor compared with the tanker
     attacks [[ref:5]]."

The Assessment's voice contract *mandates* this. ``THREADS, WEIGHTED,
UNCROWNED`` tells the model to carry two to four threads and *"say how they
weigh against each other"*, and ``ASSESSMENT_BODY_SHAPE`` item 1 says an
unranked roll of threads *"is a roll call, not a bottom line"*. The composition
rubric then grades each claim against the sub-claim its marker names — and no
single bounded block states a comparison ACROSS blocks, so the judge returns
``unsupported`` on the exact sentence the channel asked for. **The channel's
voice contract and its grader were in direct tension**, and D-6 §6.1 named the
fix as *"the ``rank`` exemption widened to the record's own order, or the rubric
taught that the assembly performs one ranking the voice may relay"*.

THE HONEST FIX IS THE SECOND ONE, and it rests on a fact about the payload
rather than on a rubric concession. **The assembly performs exactly one
cross-block ranking and it PUBLISHES it, twice over:**

  * the ORDER. ``blocks[].ordinal`` follows the ranking by construction — the
    ``cited_mass.v1`` key at sd 1.673 on the live candidate pool, over the whole
    day's pool, with the carried set its strict prefix. ``judge_input_checks``
    says so in terms: *"the ordering IS the payload"*. Ordinal 1 is the record's
    own top-weight block, and it is not an opinion.
  * the EARNED-LEAD VERDICT. ``lead.test`` publishes ``top_share``,
    ``ratio_12``, both bars, ``n_candidates`` and ``earned``, and
    ``render_lead_test`` prints it into the record's arithmetic block VERBATIM
    ("concentration EARNED this cycle" / "concentration NOT earned this cycle",
    "Lead state: …; lead ordinals: …").

P1 (``fa3d2d85``) put that whole arithmetic block into EVERY cited ordinal's
``evidence_text``, under the rule :data:`ARITHMETIC_RULE`. So a weighing sentence
that names the blocks it weighs has its truthmaker in its own evidence map — it
is relaying a ranking the record performed, not minting one. That is the licence,
and it is SUPPORTED BY CONSTRUCTION rather than by a judge's opinion.

────────────────────────────────────────────────────────────────────────────
THE LICENCE, in full. All four conditions, and each one can withhold it:

  1. **THE ARITHMETIC REF TRAVELS.** At least one ordinal the claim cites
     resolves to an evidence entry carrying :data:`ARITHMETIC_RULE`. This is the
     POPULATION FENCE and it is the reason this module is inert everywhere else:
     no desk head, no composition and no assembly row has ever carried that rule
     in a citation — only ``assessment_channel.assessment_evidence_text`` writes
     it. A comparison whose cited entries carry no arithmetic **stays as
     graded**.

  2. **BOTH COMPARED BLOCKS ARE NAMED** (or the shape does not need two). Three
     licensed shapes, each tied to a different published fact:

       * ``order`` — a weighing that is not a crowning, citing TWO OR MORE
         ordinals. The record ranked them against each other and printed the
         result as their ordinals; the voice is relaying that. A weighing that
         names ONE side and asserts a comparison against unnamed others is
         **not** licensed: the reader cannot check it, and the brief's own
         wording is *"cites the arithmetic ref AND both compared blocks"*.
       * ``earned_lead`` — a CROWNING whose cited ordinals are the record's own,
         which is true two ways: the arithmetic says concentration was EARNED
         and the crowned ordinals are inside ``lead ordinals``, or the claim
         crowns the record's top-ranked block (ordinal 1) and is therefore
         relaying the order's own head. This is ``assessment_unsupported``'s
         third ``rank`` exemption, widened from the marker surface to the
         grader, which is exactly what D-6 §6.1 asked for.
       * ``refusal`` — a NEGATED weighing ("no single driver eclipsing the
         others", "neither outweighs the other") on a record whose arithmetic
         says concentration was NOT earned. The record refused to crown; the
         sentence relays the refusal. Marking it unsupported taxed the honest
         sentence and rewarded the crowned one, which is the failure this whole
         program exists to reverse.

  3. **THE NUMBERS MATCH THE ARITHMETIC.** Every numeric token the claim states
     — after masking ``[[ref:N]]`` markers and ``block N`` ordinal references,
     neither of which is a quantity — must appear in the arithmetic block under
     the shared fold. This is the condition that keeps the licence from becoming
     a blanket pardon: *"this high-mass (9.10) and well-verified (0.85) read
     outweighs the U.S. diesel-price pressure (cited mass 5.05, verify 0.60)"*
     is a weighted comparison AND a real failure, because 9.10 / 0.85 / 5.05 /
     0.60 are on a BLOCK'S ATTRIBUTION LINE, which does not travel with the
     evidence. It is denied here and stays failing, as it must.

  4. **NO INSTRUMENT PROSE THE EVIDENCE CANNOT CARRY.** A weighing that reaches
     for the machine's vocabulary — "cited mass", "verification score", "block
     counts", what the tier "lacks" — and uses a term that appears NOWHERE in
     the evidence it cites is denied for the same reason ``instrument_prose``
     marks it: the truthmaker is on the record's rendered page and not in the
     map. The live 2026-09-06 specimen *"Blocks 3-8 … carry far smaller cited
     mass and thus contribute little to the overall risk weighting"* is exactly
     this: a weighted comparison resting on a number the map does not hold.

────────────────────────────────────────────────────────────────────────────
WHAT THIS IS NOT. It is not a claim that the voice's weighing is CORRECT — no
deterministic check can decide that, and this one does not pretend to. It is the
narrower and checkable statement that **a cross-block comparison is a claim the
record's own arithmetic licenses the voice to make**, which is precisely the
proposition the judge was answering "no" to for the wrong reason. Direction is
checked only where the record published a direction (the crowning cases); a
weighing between two named blocks is licensed as a SHAPE, and the report says
so rather than implying more.

WHY IT LIVES HERE and not in ``verify.py``. The ceiling had 49 lines and this is
a subsystem. ``verify`` imports it ONE WAY and calls it in the post-judge fold
chain beside ``_fold_metadata_claims`` — the same position and the same reason:
the judge structurally cannot decide the class (it is told to grade a claim
against the sub-claim its marker names, and the comparison is across two), so
the decision has to be made outside it.

WHY THE VOCABULARY IS RESTATED rather than imported. ``data.provenance`` must
not import ``data.analysts`` — the edge runs the other way, which is why
``assembly_arms`` restates ``assembly_spans``' scope tokens and holds them with
a test instead of an import. The same discipline applies to
:data:`ARITHMETIC_RULE`, :data:`_CROWNING_TERMS` and :data:`_INSTRUMENT_TERMS`
here, and ``test_assessment_weighting_g3.py`` pins each against the module that
owns it, so a lexicon edit on either side fails loudly rather than silently
drifting.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .composition_integrity import ref_ordinals
from .text_fold import normalize_for_match

logger = logging.getLogger(__name__)


def _verify():
    """Lazy accessor — this module is imported BY verify, so the edge runs one
    way at call time only (the ``composition_integrity`` / ``assembly_arms``
    pattern)."""
    from . import verify

    return verify


#: The rule ``assessment_channel.assessment_evidence_text`` writes between a
#: block's quoted span and the record's own arithmetic. RESTATED (see the
#: banner's last paragraph) and pinned against the owner by a test. It is the
#: population fence: an evidence entry carrying this rule is an Assessment
#: citation and nothing else in the tree has ever produced one.
ARITHMETIC_RULE: str = "--- THE RECORD THIS BLOCK SITS IN ---"

#: The licence was granted. A receipts counter, always bumped — including on the
#: rows where it changes nothing — so "how often did this fire" is a number.
WEIGHTED_COMPARISON_LICENSED: str = "weighted_comparison_licensed"

#: The licence was granted over a HARD verdict (``judge_contradicted``). Counted
#: apart from the soft lifts because it is the only way this train can move the
#: SEVERITY family, and a reader must be able to size that separately.
WEIGHTED_COMPARISON_LICENSED_HARD: str = "weighted_comparison_licensed_hard"

#: DENIALS, one counter each, because the four reasons are four different
#: findings about a body and pooling them would hide which one is live.
WEIGHTED_COMPARISON_DENIED_UNPAIRED: str = "weighted_comparison_denied_unpaired"
WEIGHTED_COMPARISON_DENIED_NUMBER: str = "weighted_comparison_denied_number"
WEIGHTED_COMPARISON_DENIED_INSTRUMENT: str = "weighted_comparison_denied_instrument"
WEIGHTED_COMPARISON_DENIED_CROWNED: str = "weighted_comparison_denied_crowned"

#: A weighted comparison was seen AMONG THE CLAIMS THE JUDGE FAILED — the
#: denominator the four denial counters and the licence counter partition
#: exactly. Scoped to the failed set on purpose: this module only ever looks at
#: a verdict it might lift, so counting comparisons the judge already SUPPORTED
#: would put a number on this row that no decision here was made about.
WEIGHTED_COMPARISON_SEEN: str = "weighted_comparison_seen"

#: The three licensed shapes, published in the override detail so a reader of the
#: ledger row can tell WHICH published fact carried the claim.
SHAPE_ORDER: str = "order"
SHAPE_EARNED_LEAD: str = "earned_lead"
SHAPE_REFUSAL: str = "refusal"

#: The verdict reasons this licence may lift. Deliberately only the two the judge
#: itself produces on a cross-block comparison: a floor reason (``no_citation``)
#: is a different defect and a deterministic arm's reason is not the judge's to
#: pardon.
_LIFTABLE_REASONS: frozenset[str] = frozenset(
    {"judge_unsupported", "judge_contradicted"}
)

# ---------------------------------------------------------------------------
# THE LEXICONS. RESTATED from ``assessment_unsupported`` (the import edge runs
# the other way) and pinned against it by test.
# ---------------------------------------------------------------------------

#: CROWNING — a claim that puts ONE subject above every other. These are the
#: entries of ``assessment_unsupported._RANK_TERMS`` that name a single winner;
#: a crowning is licensed only where the record itself crowned (see the banner's
#: ``earned_lead`` shape).
_CROWNING_TERMS: tuple[str, ...] = (
    "the top risk", "the top story", "the top concern", "the top driver",
    "the most consequential", "the most significant", "the most important",
    "the most serious", "the most urgent", "the biggest", "the largest",
    "the gravest", "the worst", "the greatest",
    "the primary driver", "the main driver", "the principal driver",
    "the key driver", "the dominant", "the leading", "the foremost",
    "the single most", "first among", "chief among", "above all",
    "defines the", "matters most", "leads this cycle",
    "the central story", "the defining",
    # The shapes the N=5 replay itself wrote. Every one crowns a single thread
    # without using a ``_RANK_TERMS`` phrase, which is why the deterministic
    # marker did not see them and the judge did.
    "the highest-weight", "the highest weight", "highest-weight thread",
    "the top global driver", "the highest-consequence",
    "the leading escalation driver",
)

#: WEIGHING WITHOUT CROWNING — the comparative vocabulary the body shape asks
#: for. A hit here licenses only when TWO OR MORE ordinals are named, because
#: the claim is about the relation between two blocks and the reader has to be
#: able to find both.
#: DELIBERATELY NARROW. Every entry is about RELATIVE WEIGHT and nothing else —
#: no bare comparatives ("larger", "ahead of"), no temporal connectives
#: ("compared with last week"), no "by contrast". A loose trigger here would let
#: the licence certify an ordinary factual comparison about the WORLD, which the
#: record's ranking says nothing about; the licence would then be doing the one
#: thing it must never do, which is pardon a claim it has no warrant over.
_WEIGHING_TERMS: tuple[str, ...] = (
    "outweighs", "outweigh", "outranks", "eclipses", "eclipsing",
    "dominates", "dominating", "takes precedence", "outstrips",
    "secondary to", "secondary factor", "secondary but", "a secondary",
    "subordinate to", "less consequential than", "more consequential than",
    "weighs more", "weighs less", "weigh against", "weighed against",
    "carries more of", "carries less of", "counts for more", "counts for less",
    "lower-weight", "higher-weight", "greater weight", "lesser weight",
    # The record's own weighting NOUNS. The N=5 replay's 09-06 specimen —
    # "Blocks 3-8 … carry far smaller cited mass and thus contribute little to
    # the overall risk weighting" — is a weighted comparison written without a
    # comparison VERB, and it must reach the denial counters rather than fall
    # through as "not in the class": it is the class, and it is denied.
    "risk weighting", "overall weighting", "the weighting",
    "contribute little", "contributes little", "contribute more",
    "contributes more", "contribute less", "contributes less",
)

#: INSTRUMENT PROSE — ``assessment_unsupported._INSTRUMENT_TERMS`` verbatim. A
#: weighing reaching for a term in here that its own evidence does not carry is
#: resting on the record's rendered page rather than on its evidence map, and
#: condition 4 denies it.
_INSTRUMENT_TERMS: tuple[str, ...] = (
    # -- the per-block attribution line -------------------------------------
    "cited mass", "citation mass", "high-mass", "low-mass",
    "verification score", "verify score", "verification level",
    "verified score", "verification rating", "verification data",
    "well-verified", "poorly-verified", "better-verified", "less-verified",
    "highly-verified", "verification above", "verification below",
    # -- the record's own shelving ------------------------------------------
    "read counts", "read count", "block counts", "block count",
    "meta-statement", "meta-statements", "meta-commentary",
    "lacks substantive", "lack substantive", "lacking substantive",
    "no substantive content", "peripheral role",
    # -- what the tier lacks ------------------------------------------------
    "the tier does not contain", "the tier does not carry",
    "the tier does not include", "the tier lacks", "this tier lacks",
    "the record does not contain", "the record does not include",
    "the record lacks", "the surface lacks", "this surface lacks",
    "the assembly lacks", "no granular data", "lacks granular",
)

#: What turns a weighing into a REFUSAL to weigh, matched in the 40 characters
#: before the hit. ``assessment_unsupported._NEGATOR_RE`` verbatim — the same
#: window, the same vocabulary, so the deterministic marker and this licence
#: agree about which sentences are refusals.
_NEGATOR_RE = re.compile(
    r"(?<![\w-])(?:no|not|neither|nor|none|never|without|hardly|nothing)(?![\w-])"
    r"|rather than|instead of",
    re.IGNORECASE,
)

#: What may stand between two words of a term in real prose — the core plane
#: writes U+2011 NON-BREAKING HYPHEN inside "highest-weight" and "high-mass"
#: (``text_fold``'s banner measures it on 58.2% of graded claims).
#: ``assessment_unsupported._FLEX_SEP`` verbatim, and for the same reason: the
#: tolerance lives in the PATTERN because the fold is not length-preserving.
_FLEX_SEP: str = r"[\s\u00a0\u202f\u2007\u2010-\u2015\u2212-]+"
_FLEX_CACHE: dict[str, re.Pattern[str]] = {}

_REF_MARKER_RE = re.compile(r"\[\[ref:\d{1,3}\]\]")

#: ``block 5`` / ``Blocks 3-8`` — an ORDINAL REFERENCE, not a quantity. Masked
#: before the number check so naming the blocks you are comparing cannot itself
#: deny the licence.
_BLOCK_ORDINAL_RE = re.compile(
    r"(?<![\w-])blocks?\s+\d{1,3}(?:\s*(?:[-–—]|to|and|,)\s*\d{1,3})*",
    re.IGNORECASE,
)

#: Every numeric token left after the two masks. Percent signs and thousands
#: separators ride along so "1,000" and "12%" are one token each.
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?%?")


def _flex_re(term: str) -> re.Pattern[str]:
    cached = _FLEX_CACHE.get(term)
    if cached is None:
        parts = [re.escape(p) for p in re.split(r"[-\s]+", term) if p]
        cached = re.compile(
            rf"(?<!\w){_FLEX_SEP.join(parts)}s?(?!\w)", re.IGNORECASE
        )
        _FLEX_CACHE[term] = cached
    return cached


def _find_flex(text: str, term: str) -> int:
    match = _flex_re(term).search(str(text or ""))
    return -1 if match is None else match.start()


def _flex_in(text: str, term: str) -> bool:
    return _flex_re(term).search(str(text or "")) is not None


def _earliest(text: str, terms: Sequence[str]) -> tuple[int, str] | None:
    """The EARLIEST lexicon hit, longest term winning a tie — the
    ``assessment_unsupported._earliest_term`` rule, so a mark and a licence talk
    about the same phrase in the same sentence."""
    best: tuple[int, str] | None = None
    for term in terms:
        at = _find_flex(text, term)
        if at < 0:
            continue
        if best is None or at < best[0] or (at == best[0] and len(term) > len(best[1])):
            best = (at, term)
    return best


# ---------------------------------------------------------------------------
# READING THE RECORD'S ARITHMETIC OUT OF THE EVIDENCE MAP
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordArithmetic:
    """The published ranking facts, read back off the evidence the claim cites.

    Nothing here is derived: every field is a phrase
    ``assessment_prompts.render_lead_test`` PRINTS into the arithmetic block,
    which ``assessment_channel`` then appends to each citation. Reading it back
    from the map rather than taking a new parameter is deliberate — it means the
    licence rests on exactly the bytes the judge was shown, and it needs no
    change at any ``verify_finding_faithfulness`` call site (``dapr_actors`` is
    untouched, which is the R4 freeze's requirement, not a convenience).
    """

    text: str
    earned: bool | None
    lead_ordinals: frozenset[int]

    @property
    def present(self) -> bool:
        return bool(self.text)


_LEAD_ORDINALS_RE = re.compile(r"lead ordinals:\s*([0-9,\s]+)", re.IGNORECASE)


def arithmetic_from_evidence(evidence: Mapping[int, str]) -> RecordArithmetic:
    """The record's arithmetic block, as carried by the cited evidence entries.

    Empty (``present`` False) for every non-Assessment finding in the tree —
    that is condition 1, and it is what makes this module inert for the desk and
    assembly families rather than merely unlikely to fire.
    """
    block = ""
    for text in evidence.values():
        raw = str(text or "")
        at = raw.find(ARITHMETIC_RULE)
        if at >= 0:
            block = raw[at + len(ARITHMETIC_RULE):]
            break
    if not block:
        return RecordArithmetic("", None, frozenset())
    folded = normalize_for_match(block)
    if "concentration earned this cycle" in folded:
        earned: bool | None = True
    elif "concentration not earned this cycle" in folded:
        earned = False
    else:
        earned = None
    match = _LEAD_ORDINALS_RE.search(block)
    ordinals = (
        frozenset(int(n) for n in re.findall(r"\d+", match.group(1)))
        if match is not None
        else frozenset()
    )
    return RecordArithmetic(block, earned, ordinals)


def _unmatched_numbers(claim: str, arithmetic: str) -> list[str]:
    """Numeric tokens the claim states that the arithmetic block does not carry.

    Condition 3. ``[[ref:N]]`` markers and ``block N`` references are masked
    first: an ordinal is a HANDLE, not a quantity, and charging a sentence for
    naming the blocks it compares would deny the licence to exactly the sentences
    that earned it.
    """
    masked = _BLOCK_ORDINAL_RE.sub(" ", _REF_MARKER_RE.sub(" ", claim))
    folded = normalize_for_match(arithmetic)
    out: list[str] = []
    for token in _NUMBER_RE.findall(masked):
        needle = normalize_for_match(token.rstrip("%").rstrip(","))
        if not needle:
            continue
        if re.search(rf"(?<![\d.]){re.escape(needle)}(?![\d])", folded) is None:
            out.append(token)
    return out


def _instrument_terms_off_map(claim: str, evidence_text: str) -> list[str]:
    """Instrument vocabulary in the claim that its own evidence does not carry.

    Condition 4. The evidence text here is the WHOLE entry — the block's quoted
    span AND the arithmetic beneath it — because that is the map the judge was
    shown, and a term the desk itself used is relayed rather than minted (the
    relay exemption, as everywhere else in this pair of modules).
    """
    return [
        term
        for term in _INSTRUMENT_TERMS
        if _find_flex(claim, term) >= 0 and not _flex_in(evidence_text, term)
    ]


# ---------------------------------------------------------------------------
# THE LICENCE
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Licence:
    """A granted licence, or a denial with the counter that records it."""

    granted: bool
    shape: str = ""
    detail: str = ""
    counter: str = ""


def licence_for(
    claim: str,
    *,
    ordinals: Sequence[int],
    evidence_text: str,
    arithmetic: RecordArithmetic,
) -> Licence | None:
    """The licence for ONE claim, or ``None`` when it is not a weighted comparison.

    ``None`` and a denial are different answers and the caller treats them
    differently: ``None`` means this sentence is not in the class at all (the
    overwhelming majority) and nothing is counted; a denial means it IS a
    weighted comparison whose licence was withheld, which is a measurement.
    """
    crowning = _earliest(claim, _CROWNING_TERMS)
    weighing = _earliest(claim, _WEIGHING_TERMS)
    hit = min(
        (h for h in (crowning, weighing) if h is not None),
        key=lambda h: h[0],
        default=None,
    )
    if hit is None:
        return None
    at, term = hit
    if not arithmetic.present:
        # Condition 1 — the fence. Not a denial and not counted: without the
        # arithmetic ref this is not an Assessment claim at all, and the module
        # has no opinion about the rest of the fleet's prose.
        return None

    # Condition 4 first: an instrument term the map cannot carry makes the whole
    # sentence unsupportable whatever its comparison shape, so it decides before
    # the shapes do (the ``_grade_one`` precedence idiom — one verdict per span).
    off_map = _instrument_terms_off_map(claim, evidence_text)
    if off_map:
        return Licence(
            False,
            counter=WEIGHTED_COMPARISON_DENIED_INSTRUMENT,
            detail=(
                f"weighted comparison resting on instrument vocabulary its "
                f"evidence does not carry ({', '.join(sorted(off_map)[:3])}) — "
                f"a block's attribution line is not in this channel's map"
            ),
        )

    # Condition 3.
    stray = _unmatched_numbers(claim, arithmetic.text)
    if stray:
        return Licence(
            False,
            counter=WEIGHTED_COMPARISON_DENIED_NUMBER,
            detail=(
                f"weighted comparison stating number(s) the record's arithmetic "
                f"does not carry ({', '.join(stray[:3])}) — the comparison may "
                f"relay the record's ranking, never a number off a block's "
                f"attribution line"
            ),
        )

    cited = sorted({int(o) for o in ordinals})
    negated = _NEGATOR_RE.search(claim[max(0, at - 40):at]) is not None

    # Condition 2 — the shapes.
    if negated:
        if arithmetic.earned is False and cited:
            return Licence(
                True, SHAPE_REFUSAL,
                detail=(
                    f"refusal to crown, relayed: the record's own arithmetic "
                    f"says concentration was NOT earned this cycle, and this "
                    f"sentence ({term!r}, negated) says so. Cited ordinal(s) "
                    f"{cited}."
                ),
            )
        return Licence(
            False,
            counter=WEIGHTED_COMPARISON_DENIED_CROWNED,
            detail=(
                "refusal to crown on a record whose arithmetic did not say so "
                "(concentration EARNED, or the verdict is absent) — the voice "
                "may disagree with the arithmetic, but not by relaying it"
            ),
        )

    if crowning is not None and crowning[0] == at:
        # A CROWNING. Licensed two ways, both published: the record crowned these
        # ordinals, or the claim crowns the record's own top-ranked block.
        if arithmetic.earned and cited and set(cited) <= set(arithmetic.lead_ordinals):
            return Licence(
                True, SHAPE_EARNED_LEAD,
                detail=(
                    f"crowning relayed from the record's EARNED lead: the "
                    f"arithmetic says concentration was earned and names lead "
                    f"ordinals {sorted(arithmetic.lead_ordinals)}; this sentence "
                    f"crowns {cited}."
                ),
            )
        if cited and min(cited) == 1:
            return Licence(
                True, SHAPE_EARNED_LEAD,
                detail=(
                    f"crowning relayed from the record's ORDER: ordinal 1 is the "
                    f"record's own top-weight block by construction "
                    f"(``cited_mass.v1``, the carried set a strict prefix of the "
                    f"ranked pool) and this sentence cites it {cited}."
                ),
            )
        return Licence(
            False,
            counter=WEIGHTED_COMPARISON_DENIED_CROWNED,
            detail=(
                f"crowning a thread the record's own order does not put first: "
                f"cited ordinal(s) {cited or 'none'}, lead ordinals "
                f"{sorted(arithmetic.lead_ordinals) or 'none'}, concentration "
                f"earned={arithmetic.earned}. The prompt forbids crowning "
                f"against the arithmetic and this is that sentence."
            ),
        )

    # A WEIGHING. Both compared blocks must be named.
    if len(cited) >= 2:
        return Licence(
            True, SHAPE_ORDER,
            detail=(
                f"cross-block weighing relayed from the record's published "
                f"order: the assembly ranked the whole candidate pool on "
                f"``cited_mass.v1`` and printed the result as the ordinals this "
                f"sentence names ({cited}). The comparison is the record's; the "
                f"voice is carrying it."
            ),
        )
    return Licence(
        False,
        counter=WEIGHTED_COMPARISON_DENIED_UNPAIRED,
        detail=(
            f"weighted comparison naming only {cited or 'no'} ordinal(s): the "
            f"record ranks blocks against each other, so a claim that one "
            f"outweighs another has to name BOTH sides for the reader to check "
            f"it against the order"
        ),
    )


# ---------------------------------------------------------------------------
# THE FOLD
# ---------------------------------------------------------------------------


def fold(report: Any, *, body: str, citations: Any) -> Any:
    """Grant the licence over one finding's already-graded claims.

    Runs AFTER the judge and for the same reason ``_fold_metadata_claims`` does:
    the judge is TOLD to grade each claim against the sub-claim its marker names,
    so a comparison across two blocks is a question it structurally cannot answer
    correctly. This is the authority on the class.

    INERT, and byte-identically so, for every finding whose citations carry no
    :data:`ARITHMETIC_RULE` — which today is every row in the tree except an
    Assessment. Never raises: a malformed citation list degrades to no licence,
    which leaves the judge's verdict standing and is the honest direction.
    """
    v = _verify()
    if not body or not v._uses_subclaim_convention(citations):
        return report
    try:
        evidence = v._ordinal_evidence_map(citations)
    except Exception as exc:  # noqa: BLE001 — degrade-not-drop, never break verify
        logger.warning("verify.assessment_weighting.setup_failed err=%s", exc)
        return report
    if not evidence:
        return report
    arithmetic = arithmetic_from_evidence(evidence)
    if not arithmetic.present:
        return report

    failed_by_text = {
        cv.text.strip(): cv
        for cv in (report.claim_verdicts or [])
        if cv.reason in _LIFTABLE_REASONS
    }
    if not failed_by_text:
        return report

    overrides: list[Any] = []
    lifted_hard = 0
    denials: dict[str, int] = {}
    seen = 0
    for claim in v._segment_claims(body):
        key = claim.strip()
        cv = failed_by_text.get(key)
        if cv is None:
            continue
        ordinals = ref_ordinals(claim)
        evidence_text = "\n".join(
            str(evidence.get(o) or "") for o in sorted(set(ordinals))
        )
        try:
            decision = licence_for(
                claim,
                ordinals=ordinals,
                evidence_text=evidence_text,
                arithmetic=arithmetic,
            )
        except Exception as exc:  # noqa: BLE001 — one bad claim never breaks a pass
            logger.warning(
                "verify.assessment_weighting.claim_failed err=%s claim=%r",
                exc, claim[:120],
            )
            continue
        if decision is None:
            continue
        seen += 1
        if not decision.granted:
            denials[decision.counter] = denials.get(decision.counter, 0) + 1
            continue
        if cv.reason == "judge_contradicted":
            lifted_hard += 1
        overrides.append(
            v._ClaimOverride(
                text=claim,
                supported=True,
                counter=WEIGHTED_COMPARISON_LICENSED,
                detail=f"[{decision.shape}] {decision.detail}",
            )
        )

    if seen:
        report.bump(WEIGHTED_COMPARISON_SEEN, seen)
    for counter, n in denials.items():
        report.bump(counter, n)
    if not overrides:
        return report
    logger.info(
        "verify.assessment_weighting.licensed n=%d hard=%d denied=%d — the "
        "record's own ranking, relayed",
        len(overrides), lifted_hard, sum(denials.values()),
    )
    out = v._apply_claim_overrides(report, overrides)
    if lifted_hard:
        out.bump(WEIGHTED_COMPARISON_LICENSED_HARD, lifted_hard)
    return _rescore_citation_branch(out, [o.text for o in overrides])


def _rescore_citation_branch(report: Any, lifted: Sequence[str]) -> Any:
    """Move ``branch_scores['citation_support']`` with the verdicts that moved.

    ``_apply_claim_overrides`` deliberately carries ``branch_scores`` through
    UNCHANGED — it is the judge's own per-kind telemetry and no existing override
    stage claims authority over it. This train does claim it, for exactly one
    branch and only for the claims it lifted, because ``citation_support`` IS the
    Assessment's ``fidelity_to_spine``: the number G3 is about and the number the
    live critique publishes. Leaving it stale would mean the row's headline moved
    and the branch a reader partitions on did not.

    Nothing else in ``branch_scores`` is touched, and a claim whose kind is not
    ``citation_support`` moves nothing here — which is what keeps the shift
    confined to the branch the lineage entry declares.
    """
    v = _verify()
    branch = (report.branch_scores or {}).get(v.CLAIM_KIND_CITATION_SUPPORT)
    if not isinstance(branch, dict):
        return report
    moved = sum(
        1 for text in lifted
        if v._claim_kind(text) == v.CLAIM_KIND_CITATION_SUPPORT
    )
    if not moved:
        return report
    checkable = int(branch.get("checkable") or 0)
    supported = min(int(branch.get("supported") or 0) + moved, checkable)
    branch["supported"] = supported
    branch["score"] = 1.0 if checkable == 0 else round(supported / checkable, 4)
    return report


__all__ = [
    "ARITHMETIC_RULE",
    "Licence",
    "RecordArithmetic",
    "SHAPE_EARNED_LEAD",
    "SHAPE_ORDER",
    "SHAPE_REFUSAL",
    "WEIGHTED_COMPARISON_DENIED_CROWNED",
    "WEIGHTED_COMPARISON_DENIED_INSTRUMENT",
    "WEIGHTED_COMPARISON_DENIED_NUMBER",
    "WEIGHTED_COMPARISON_DENIED_UNPAIRED",
    "WEIGHTED_COMPARISON_LICENSED",
    "WEIGHTED_COMPARISON_LICENSED_HARD",
    "WEIGHTED_COMPARISON_SEEN",
    "arithmetic_from_evidence",
    "fold",
    "licence_for",
]
