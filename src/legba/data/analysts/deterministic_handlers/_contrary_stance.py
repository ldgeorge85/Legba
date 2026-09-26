# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE STANCE — what the FETCHED PAGES say, derived in code, never judged.

THE ONE RULE THIS MODULE ENFORCES. ``stance`` is a statement about the
RETRIEVAL, not about the claim. "A page the platform holds asserts the opposite"
is a fact about a page; "the claim is false" is a verdict, and this pass does
not render verdicts — the capture is explicit ("Emit contention records, never
verdicts") and the platform already carries two graders whose populations must
not be pooled with a third. So no model is asked anything here. The stance falls
out of the claim's own polarity against the polarity of a sentence on a page we
fetched, by the same closed vocabulary R2 calibrated.

THE FOUR STANCES, AND WHY THE LAST TWO ARE NOT THE SAME THING:

  ``contradicts``    a fetched page asserts the opposite state, on the claim's
                     own subject.
  ``qualifies``      a fetched page agrees in direction but NARROWS the claim —
                     partially, briefly, in one place, under a condition. A
                     qualification is the commonest real finding of a contrary
                     pass and folding it into "contradicts" would overstate
                     every one of them.
  ``none_found``     the search answered, pages were read, nothing was
                     admissible.
  ``search_failed``  the retrieval did not happen — no counter-query, a refused
                     search plane, every fetch failed. The audit's own
                     UNCHECKED/NOT_FOUND split, one level down: "we looked and
                     found nothing" and "we could not look" must never share a
                     shape, because only the first is evidence of anything.

TWO DERIVATIONS, AND THEY DO NOT CARRY THE SAME WEIGHT
-------------------------------------------------------
``polarity`` — the claim takes a side in R2's table, the page sentence takes the
other side of the same group on the same subject. That table was calibrated
against the live corpus: 1,592 verified claims, a first cut of 57 pairs across
24 of 32 desks, almost all false, pruned to 0. Four rules closed that gap and
all four are reused here — a polarity term must sit NEXT TO the shared subject
(``SUBJECT_PROXIMITY``), long sentences are not single propositions
(``MAX_CLAIM_CHARS``), scaffold words are not subjects, and the vocabulary stays
state-oppositions only.

``negation`` — the claim takes no side in that table (most claims do not; the
table is narrow on purpose). The fallback looks for the one shape that is still
decidable without a vocabulary: a page sentence that DENIES a proposition the
claim asserts, or asserts one the claim denies, anchored on at least
:data:`NEGATION_MIN_OVERLAP` shared content tokens with the negator inside R2's
own window.

THIS FALLBACK IS NOT CALIBRATED, AND THE DESIGN SAYS SO IN CODE. R2 earned its
precision with a live sweep; this rule has had no such sweep, and an arbitrary
web page is a far noisier substrate than a verified claim. So the fallback is
STRICTER than R2 on every dial it shares (three shared tokens rather than two,
the negator anchored by proximity rather than position alone) and — the part
that matters — its records are WITHHELD FROM THE COMPOSITION. The composition
tension rule admits ``derivation='polarity'`` only. A false ``contradicts``
rendered into a composition manufactures a disagreement for the fleet to write
up, which is precisely the outcome R2 shipped zero pairs rather than risk; a
false ``contradicts`` on a route and an Inspector chip is a line a human reads
with its counter-ref one click away. The two are not the same exposure and this
module does not pretend they are.

``none_found`` IS THE EXPECTED READING. A day on which this pass finds nothing
is the normal day, exactly as it is for R2 and for the standing auditor's
NOT_FOUND. Anyone tuning these rules toward a livelier number is tuning them
toward the 57 false pairs.

AND THEN THE FOUR FENCES, BECAUSE THE FIRST LIVE RUN SAID SO
-------------------------------------------------------------
The rules above are the SENTENCE rules, and on 2026-09-25 they produced three
``contradicts`` rows and all three were false — two encyclopedia articles and a
lone think-tank piece from another month. The sentence was never the whole
question: an admissible counter page also has to be a REPORTING page, DATED
INSIDE the claim's window, ABOUT the claim's subject, and NOT ALONE.

:mod:`._contrary_fences` is those four questions and every one of its answers is
a number that lands on the row — ``host_class``, ``page_published_at``,
``subject_overlap``, ``independent_pages``. They run HERE rather than at the
handler so that no caller can derive a stance without them: a fence a caller can
forget to apply is a default, not a fence. :func:`derive_stance` takes the
claim's window and the host catalog; with neither, F1, F3 and F4 still run and
F2 still refuses an undated page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..claim_contradiction import (
    MAX_CLAIM_CHARS,
    MIN_SUBJECT_OVERLAP,
    NEGATION_WINDOW,
    NEGATORS,
    SUBJECT_PROXIMITY,
    polarity_of,
    subject_tokens_of,
    tokenize,
)
from ._contrary_fences import (
    FENCE_INDEPENDENT_PAGES,
    HOST_CLASS_UNKNOWN,
    ClaimWindow,
    PageVerdict,
    as_iso,
    contradiction_admitted,
    host_class_of,
    independent_pages,
    judge_page,
    page_date_of,
)

#: ``stance`` values — the table's CHECK vocabulary.
STANCE_CONTRADICTS = "contradicts"
STANCE_QUALIFIES = "qualifies"
STANCE_NONE_FOUND = "none_found"
STANCE_SEARCH_FAILED = "search_failed"

STANCES: tuple[str, ...] = (
    STANCE_CONTRADICTS, STANCE_QUALIFIES, STANCE_NONE_FOUND,
    STANCE_SEARCH_FAILED,
)

#: ``derivation`` values.
DERIVATION_POLARITY = "polarity"
DERIVATION_NEGATION = "negation"
DERIVATION_NONE = "none"

#: The one derivation a composition may read. See the module docstring.
COMPOSITION_DERIVATIONS: frozenset[str] = frozenset({DERIVATION_POLARITY})

#: The negation fallback's own floor — THREE shared content tokens, against
#: R2's two. R2's two is defensible over a desk's verified claims, which are
#: written in one register about one unit; a page from the open web is not, and
#: the extra token is the cheapest available precision.
NEGATION_MIN_OVERLAP = 3

#: How much of a page one stance derivation reads. The fetch leg already caps a
#: page at 200,000 chars; this bounds the SENTENCE scan, so a pathological page
#: cannot turn one claim into a long CPU walk inside a bounded tick.
MAX_PAGE_SENTENCES = 400

#: The quoted evidence carried onto the row and into a tension line.
MAX_QUOTE_CHARS = 240

#: QUALIFIERS — words that NARROW a state rather than deny it.
#:
#: Deliberately small and deliberately distinctive. The tempting additions are
#: the common ones ("only", "some", "single"), and they are exactly the ones
#: that would fire on ordinary prose — the same shape as the ``trend`` and
#: ``stable``/``steady`` terms R2 had to prune, which matched everywhere and
#: therefore meant nothing. ``test_qualifiers_are_disjoint_from_the_polarity_
#: vocabulary`` pins that no word here is also a polarity term or a stopword, so
#: a qualifier can never quietly become the thing it is qualifying.
QUALIFIERS: frozenset[str] = frozenset({
    "partially", "partial", "briefly", "temporarily", "temporary",
    "intermittent", "intermittently", "sporadic", "sporadically",
    "provisional", "provisionally", "conditional", "conditionally",
    "localised", "localized", "short-lived", "narrowly", "nominally",
})

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class PageRef:
    """One fetched page, as a contention row's ``refs`` entry sees it.

    The last four fields are the FENCES' record of this page, and they are on
    the ref rather than only on the row because a claim can be fetched two pages
    and a reader has to be able to see which one was refused and by what.
    ``fence`` is ``""`` for an admissible page; ``published_at`` stays the fetch
    leg's raw discovery while ``page_published_at`` is what the date gate
    actually parsed out of it (or out of a dated URL), so a malformed stamp is
    visible as the difference between the two rather than silently becoming a
    date.
    """

    url: str
    sha256: str
    chars: int
    published_at: str | None = None
    extracted: bool = True
    fetched_at: str = ""
    status_code: int | None = None
    stance: str = STANCE_NONE_FOUND
    quote: str = ""
    host_class: str = HOST_CLASS_UNKNOWN
    page_published_at: str | None = None
    subject_overlap: int = 0
    fence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "sha256": self.sha256,
            "chars": int(self.chars),
            "published_at": self.published_at,
            "extracted": bool(self.extracted),
            "fetched_at": self.fetched_at,
            "status_code": self.status_code,
            "stance": self.stance,
            "quote": self.quote[:MAX_QUOTE_CHARS],
            "host_class": self.host_class,
            "page_published_at": self.page_published_at,
            "subject_overlap": int(self.subject_overlap),
            "fence": self.fence,
        }


@dataclass(frozen=True)
class StanceOutcome:
    """The claim's stance across every page that was fetched for it.

    ``host_class`` / ``page_published_at`` / ``subject_overlap`` describe the
    DECISIVE page — the one the statement quotes — and are null/zero when no
    page was decisive. ``independent_pages`` is a CLAIM-level count (F4): how
    many independent outlets carried an admissible contradiction, which is what
    ``contradicts`` needs two of. ``fences`` names every fence that changed this
    outcome, so a receipt can stratify a week of runs by which rule is doing the
    work rather than by how many rows it left behind.
    """

    stance: str = STANCE_NONE_FOUND
    derivation: str = DERIVATION_NONE
    statement: str = ""
    quote: str = ""
    url: str = ""
    refs: tuple[PageRef, ...] = field(default_factory=tuple)
    host_class: str = ""
    page_published_at: str | None = None
    subject_overlap: int = 0
    independent_pages: int = 0
    fences: tuple[str, ...] = field(default_factory=tuple)

    @property
    def stance_bearing(self) -> bool:
        return self.stance in (STANCE_CONTRADICTS, STANCE_QUALIFIES)

    @property
    def composition_visible(self) -> bool:
        """Whether a composition may render this. See the module docstring."""
        return (
            self.stance == STANCE_CONTRADICTS
            and self.derivation in COMPOSITION_DERIVATIONS
        )


# ---------------------------------------------------------------------------
# Sentence-level rules
# ---------------------------------------------------------------------------


def sentences(text: str, *, cap: int = MAX_PAGE_SENTENCES) -> list[str]:
    """Bounded sentence split. Single-proposition sentences only.

    A sentence past :data:`MAX_CLAIM_CHARS` is dropped for R2's stated reason: a
    400-character sentence enumerating five domains cannot be assigned one
    polarity, and trying is how a detector invents disagreement.
    """
    out: list[str] = []
    for raw in _SENTENCE_SPLIT.split(text or ""):
        if len(out) >= cap:
            break
        line = _WS.sub(" ", raw).strip()
        if line and len(line) <= MAX_CLAIM_CHARS:
            out.append(line)
    return out


def _negated_tokens(tokens: Sequence[str]) -> set[str]:
    """Tokens carrying a negator within R2's window BEFORE them."""
    out: set[str] = set()
    for i, tok in enumerate(tokens):
        window = tokens[max(0, i - NEGATION_WINDOW):i]
        if any(w in NEGATORS for w in window):
            out.add(tok)
    return out


def _qualifier_near(tokens: Sequence[str], anchors: frozenset[str]) -> bool:
    """A qualifier sitting within :data:`SUBJECT_PROXIMITY` of a shared token.

    Proximity, not mere presence: "the crossing reopened" in a paragraph that
    elsewhere says "temporarily" about something else is not a qualification of
    this claim, and R2's own load-bearing precision rule is exactly this one.
    """
    for i, tok in enumerate(tokens):
        if tok not in QUALIFIERS:
            continue
        near = tokens[max(0, i - SUBJECT_PROXIMITY):i + SUBJECT_PROXIMITY + 1]
        if any(w in anchors for w in near):
            return True
    return False


def stance_of_sentence(
    claim: str,
    sentence: str,
    *,
    group: str | None,
    sign: int | None,
) -> tuple[str, str]:
    """``(stance, derivation)`` for ONE page sentence against ONE claim.

    ``(none_found, none)`` when the sentence settles nothing — which is the
    answer for the overwhelming majority of sentences on the overwhelming
    majority of pages, and is not a failure.
    """
    if not sentence or len(sentence) > MAX_CLAIM_CHARS:
        return STANCE_NONE_FOUND, DERIVATION_NONE
    claim_subject = subject_tokens_of(claim)
    page_subject = subject_tokens_of(sentence)
    shared = claim_subject & page_subject
    page_tokens = tokenize(sentence)

    # -- the calibrated rule ------------------------------------------------
    if group and sign:
        if len(shared) < MIN_SUBJECT_OVERLAP:
            return STANCE_NONE_FOUND, DERIVATION_NONE
        page_polarity = polarity_of(sentence, anchors=shared)
        page_sign = page_polarity.get(group)
        if page_sign is not None and page_sign != sign:
            return STANCE_CONTRADICTS, DERIVATION_POLARITY
        if page_sign == sign and _qualifier_near(page_tokens, shared):
            return STANCE_QUALIFIES, DERIVATION_POLARITY
        return STANCE_NONE_FOUND, DERIVATION_NONE

    # -- the uncalibrated fallback -----------------------------------------
    if len(shared) < NEGATION_MIN_OVERLAP:
        return STANCE_NONE_FOUND, DERIVATION_NONE
    claim_tokens = tokenize(claim)
    claim_negated = _negated_tokens(claim_tokens) & shared
    page_negated = _negated_tokens(page_tokens) & shared
    # A shared proposition one side denies and the other does not. Symmetric on
    # purpose: "X did not resign" against "X resigned" is the same defect as its
    # mirror, and a rule that saw only one direction would be blind to whichever
    # way this fleet happens to phrase things.
    if claim_negated ^ page_negated:
        return STANCE_CONTRADICTS, DERIVATION_NEGATION
    if _qualifier_near(page_tokens, shared):
        return STANCE_QUALIFIES, DERIVATION_NEGATION
    return STANCE_NONE_FOUND, DERIVATION_NONE


# ---------------------------------------------------------------------------
# Page-level and claim-level
# ---------------------------------------------------------------------------

#: Strongest first. A page that contradicts outranks one that qualifies, and a
#: calibrated derivation outranks the fallback at equal stance.
_STANCE_RANK = {
    (STANCE_CONTRADICTS, DERIVATION_POLARITY): 4,
    (STANCE_CONTRADICTS, DERIVATION_NEGATION): 3,
    (STANCE_QUALIFIES, DERIVATION_POLARITY): 2,
    (STANCE_QUALIFIES, DERIVATION_NEGATION): 1,
}


def stance_of_page(
    claim: str, page_text: str, *, group: str | None, sign: int | None,
) -> tuple[str, str, str]:
    """``(stance, derivation, quote)`` — the STRONGEST sentence on one page."""
    best = (STANCE_NONE_FOUND, DERIVATION_NONE, "")
    best_rank = 0
    for sentence in sentences(page_text):
        stance, derivation = stance_of_sentence(
            claim, sentence, group=group, sign=sign
        )
        rank = _STANCE_RANK.get((stance, derivation), 0)
        if rank > best_rank:
            best_rank = rank
            best = (stance, derivation, sentence[:MAX_QUOTE_CHARS])
    return best


def _subject_phrase(claim: str, limit: int = 4) -> str:
    ordered: list[str] = []
    subject = subject_tokens_of(claim)
    for tok in tokenize(claim):
        if tok in subject and tok not in ordered:
            ordered.append(tok)
        if len(ordered) >= limit:
            break
    return " ".join(ordered)


def statement_for(
    claim: str, stance: str, derivation: str, ref: PageRef,
) -> str:
    """The hedged line a reader sees. DECLARES, never explains, never resolves.

    The register is the assembly tier's own: it states THAT a retrieved page
    takes a position and WHICH page, and it stops. It may not say the claim is
    wrong, may not weigh the two, and may not name a winner — the arbitration
    this pass performs is exactly none, and a sentence that implied otherwise
    would be a verdict wearing a record's clothes.
    """
    subject = _subject_phrase(claim) or "this claim"
    # The DATE GATE's date, not the raw discovery: what the reader is shown is
    # the date the fence actually parsed, so a stamp the gate could not read
    # renders as absence rather than as a string nobody validated.
    dated = ref.page_published_at or ref.published_at
    when = f", published {dated}" if dated else ""
    verb = (
        "states the opposite" if stance == STANCE_CONTRADICTS
        else "narrows this"
    )
    return (
        f"retrieved counter-evidence on {subject} {verb}: "
        f'"{ref.quote[:MAX_QUOTE_CHARS]}" — {ref.url}{when}; retrieved '
        f"{ref.fetched_at or 'this pass'} on the free rung"
        f"{' (uncalibrated negation rule)' if derivation == DERIVATION_NEGATION else ''}. "
        f"Not adjudicated: this record says what was retrieved, not which side is right."
    )


def overlap_floor(derivation: str) -> int:
    """F3's floor for one derivation — the rule's OWN dial, not a new one.

    The calibrated leg keeps R2's two; the uncalibrated fallback keeps its own
    three. F3 does not invent a third number, it makes these two visible on the
    row and refuses the page that falls under them.
    """
    if derivation == DERIVATION_NEGATION:
        return NEGATION_MIN_OVERLAP
    return MIN_SUBJECT_OVERLAP


def derive_stance(
    claim: str,
    pages: Sequence[Mapping[str, Any]],
    *,
    group: str | None = None,
    sign: int | None = None,
    window: ClaimWindow | None = None,
    catalog: Mapping[str, str] | None = None,
) -> StanceOutcome:
    """The claim's stance over every page fetched for it, PAST THE FOUR FENCES.

    ``pages`` entries carry ``text`` plus the fetch fence fields
    (``url`` / ``sha256`` / ``chars`` / ``published_at`` / ``extracted`` /
    ``fetched_at`` / ``status_code``). Every page becomes a ``refs`` entry,
    including the ones that settled nothing — a record that shows WHICH pages
    were read and found silent is a far better record than one that shows only
    the hits, and it is what makes ``none_found`` believable.

    ``window`` is the claim's admissible counter-evidence window (F2); ``None``
    means UNMEASURED, which still refuses an undated page but admits a dated one
    — an unmeasured window is not an out-of-window page. ``catalog`` is the
    ``{host: source_class}`` map F1 labels a known outlet from; without it every
    non-reference host is ``unknown``, which passes.

    THE FENCES RUN IN TWO PLACES AND THAT IS THE DESIGN. F1–F3 are properties of
    ONE page, so they run per page and their verdict rides that page's ref. F4
    is a property of the CLAIM's whole evidence set — "is there a second,
    independent page saying this" — so it can only run once every page has been
    judged, and it caps the claim's stance without rewriting any page's own.
    """
    fence_window = window or ClaimWindow()
    refs: list[PageRef] = []
    contradicting: list[Mapping[str, Any]] = []
    best_ref: PageRef | None = None
    best_stance = STANCE_NONE_FOUND
    best_derivation = DERIVATION_NONE
    best_rank = 0
    fired: list[str] = []
    for raw in pages or ():
        text = str(raw.get("text") or "")
        url = str(raw.get("url") or "")
        raw_date = raw.get("published_at")
        stance, derivation, quote = stance_of_page(
            claim, text, group=group, sign=sign
        )
        if stance in (STANCE_CONTRADICTS, STANCE_QUALIFIES):
            verdict = judge_page(
                claim, url=url, published_at=raw_date, sentence=quote,
                window=fence_window, min_overlap=overlap_floor(derivation),
                catalog=catalog,
            )
            if verdict.rejected:
                stance, derivation = STANCE_NONE_FOUND, DERIVATION_NONE
            elif verdict.demoted and stance == STANCE_CONTRADICTS:
                stance = STANCE_QUALIFIES
            if verdict.fence and verdict.fence not in fired:
                fired.append(verdict.fence)
        else:
            # A page that settled nothing is still LABELLED — what the free rung
            # handed back is the run's own search-health evidence, and the first
            # live run's answer to that was "encyclopedias". No fence fired on
            # it, so none is recorded.
            page_date, date_source = page_date_of(url, raw_date)
            verdict = PageVerdict(
                host_class=host_class_of(url, catalog),
                page_published_at=page_date, date_source=date_source,
            )
        ref = PageRef(
            url=url,
            sha256=str(raw.get("sha256") or ""),
            chars=int(raw.get("chars") or len(text)),
            published_at=(str(raw_date) if raw_date else None),
            extracted=bool(raw.get("extracted", True)),
            fetched_at=str(raw.get("fetched_at") or ""),
            status_code=(
                int(raw["status_code"]) if raw.get("status_code") is not None
                else None
            ),
            stance=stance,
            quote=quote,
            host_class=verdict.host_class,
            page_published_at=as_iso(verdict.page_published_at),
            subject_overlap=verdict.subject_overlap,
            fence=verdict.fence,
        )
        refs.append(ref)
        if stance == STANCE_CONTRADICTS:
            contradicting.append(raw)
        rank = _STANCE_RANK.get((stance, derivation), 0)
        if rank > best_rank:
            best_rank = rank
            best_ref, best_stance, best_derivation = ref, stance, derivation

    # -- F4, the CLAIM-level fence ------------------------------------------
    independent = independent_pages(contradicting)
    if best_stance == STANCE_CONTRADICTS and not contradiction_admitted(
        independent
    ):
        best_stance = STANCE_QUALIFIES
        if FENCE_INDEPENDENT_PAGES not in fired:
            fired.append(FENCE_INDEPENDENT_PAGES)

    if best_ref is None or best_rank == 0:
        return StanceOutcome(
            refs=tuple(refs), independent_pages=independent,
            fences=tuple(fired),
        )
    return StanceOutcome(
        stance=best_stance,
        derivation=best_derivation,
        statement=statement_for(claim, best_stance, best_derivation, best_ref),
        quote=best_ref.quote,
        url=best_ref.url,
        refs=tuple(refs),
        host_class=best_ref.host_class,
        page_published_at=best_ref.page_published_at,
        subject_overlap=best_ref.subject_overlap,
        independent_pages=independent,
        fences=tuple(fired),
    )


def failed(refs: Sequence[PageRef] = ()) -> StanceOutcome:
    """The ``search_failed`` outcome — the retrieval did not happen.

    Carries NO reason of its own, deliberately: the reason belongs on the row's
    own ``reason`` column, where a week of rows stratifies by it. An outcome
    object holding a second copy would be a second place for the two to
    disagree.
    """
    return StanceOutcome(
        stance=STANCE_SEARCH_FAILED, derivation=DERIVATION_NONE,
        statement="", refs=tuple(refs),
    )


def qualifiers_are_disjoint() -> list[str]:
    """Qualifier words that are ALSO polarity terms. Empty is the only pass.

    Returned rather than asserted so a test names every offender at once.
    """
    from ..claim_contradiction import POLARITY_GROUPS, polarity_side_terms

    polarity_terms: set[str] = set()
    for group in POLARITY_GROUPS:
        polarity_terms |= set(polarity_side_terms(group, 1))
        polarity_terms |= set(polarity_side_terms(group, -1))
    return sorted(QUALIFIERS & polarity_terms)


__all__ = [
    "COMPOSITION_DERIVATIONS",
    "ClaimWindow",
    "DERIVATION_NEGATION",
    "DERIVATION_NONE",
    "DERIVATION_POLARITY",
    "MAX_PAGE_SENTENCES",
    "MAX_QUOTE_CHARS",
    "NEGATION_MIN_OVERLAP",
    "QUALIFIERS",
    "STANCE_CONTRADICTS",
    "STANCE_NONE_FOUND",
    "STANCE_QUALIFIES",
    "STANCE_SEARCH_FAILED",
    "STANCES",
    "PageRef",
    "StanceOutcome",
    "derive_stance",
    "failed",
    "overlap_floor",
    "qualifiers_are_disjoint",
    "sentences",
    "stance_of_page",
    "stance_of_sentence",
    "statement_for",
]
