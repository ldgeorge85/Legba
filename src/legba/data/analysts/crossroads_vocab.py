# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The crossroads DICTIONARY — what the four detectors treat as a term, a
direction and a citation. Pure: no I/O, no clock, no connection.

SPLIT OUT OF :mod:`crossroads_detectors` DELIBERATELY, for the reason
:mod:`unit_names` states about itself: this half is a claim about WORDS and the
other half is a claim about ARITHMETIC, and the two rot at different rates. A
reviewer arguing with :data:`COMMODITY_TERMS` or :data:`POLARITY_LEXICON` —
which is exactly the argument a DECLARED vocabulary exists to invite — should
not have to read a fetcher to do it, and moving a detector threshold should not
touch the file the vocabularies live in.

THE POSTURE IS PRECISION, everywhere, for the reason ``spread_block`` states:
a missed term costs a sentence the crossroads did not write, while a FALSE term
manufactures a convergence out of ordinary vocabulary and hands it to a model
under instructions to narrate it. Hence declared vocabularies rather than
inferred classes; title-only extraction; no proper noun at all out of a
Title-Cased headline; and a single-word LEAD that keys only on corpus evidence
(:func:`proper_candidates`).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Term extraction — the PATTERNS vocabulary.
# ---------------------------------------------------------------------------

#: A commodity term the detector keys on. DECLARED, not inferred: the module
#: names its own vocabulary so a reviewer disagrees with a list rather than with
#: a classifier. Multi-word entries are matched as whole phrases over the
#: normalised title. Deliberately narrow — a term here must be one a desk would
#: only write when it means the commodity.
COMMODITY_TERMS: frozenset[str] = frozenset({
    "crude", "brent", "wti", "diesel", "gasoline", "jet fuel", "naphtha",
    "natural gas", "lng", "pipeline gas", "coal", "uranium", "enriched uranium",
    "wheat", "grain", "maize", "soybeans", "rice", "sugar", "palm oil",
    "fertiliser", "fertilizer", "potash", "urea", "ammonia",
    "copper", "cobalt", "lithium", "nickel", "rare earths", "graphite",
    "semiconductors", "chips", "lithography", "steel", "aluminium", "aluminum",
    "container freight", "bunker fuel", "refining capacity",
})

#: Capitalised words that are never an entity. A candidate that normalises into
#: this set is dropped whole; a MULTI-word candidate whose every word is in here
#: is dropped too (``The Ministry`` is not an actor).
TERM_STOPWORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "with", "by", "from", "as", "after", "before", "amid", "over", "under",
    "new", "more", "most", "first", "second", "third", "next", "last",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "president", "minister", "ministry", "government", "parliament", "state",
    "report", "reports", "reuters", "update", "analysis", "statement",
    "officials", "official", "sources", "source", "week", "month", "year",
})

#: A candidate must be at least this long after normalisation — a two-letter
#: capitalised token is an initial or a code, not a checkable subject.
MIN_TERM_CHARS: int = 4

#: A title with at least this share of capitalised words is Title Case, where
#: capitalisation carries no information — it yields NO proper-noun candidates.
TITLE_CASE_RATIO: float = 0.6

#: Words per proper-noun run, capped so an all-caps fragment cannot key.
MAX_TERM_WORDS: int = 4

#: A capitalised run: an initial capital plus internal lowercase connectors.
_PROPER_RUN_RE = re.compile(
    r"\b[A-Z][\w'’\-]*(?:\s+(?:of|the|and|de|al|el|bin|van|von)\s+[A-Z][\w'’\-]*"
    r"|\s+[A-Z][\w'’\-]*)*"
)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


# ---------------------------------------------------------------------------
# Polarity — the CONTRADICTIONS lexicon.
# ---------------------------------------------------------------------------

#: A DECLARED directional lexicon, same posture as the commodity vocabulary. A
#: title's polarity is the sign of the sum of its hits; a tie is polarity ZERO
#: (no claim), never a coin flip. Word-boundary matched over the normalised
#: title, so "escalation" and "escalating" both need their own entry — an
#: explicit list beats a stemmer whose mistakes are invisible.
POLARITY_LEXICON: Mapping[str, int] = {
    # escalatory / deteriorating
    "escalates": 1, "escalating": 1, "escalation": 1, "escalatory": 1,
    "deteriorates": 1, "deteriorating": 1, "deterioration": 1,
    "intensifies": 1, "intensifying": 1, "worsens": 1, "worsening": 1,
    "widens": 1, "widening": 1, "surges": 1, "surging": 1,
    "collapse": 1, "collapses": 1, "collapsing": 1, "breakdown": 1,
    "rising": 1, "rises": 1, "mounting": 1, "hardens": 1, "hardening": 1,
    # de-escalatory / improving
    "de-escalates": -1, "deescalates": -1, "de-escalation": -1,
    "deescalation": -1, "de-escalating": -1, "deescalating": -1,
    "eases": -1, "easing": -1, "easement": -1, "improves": -1,
    "improving": -1, "improvement": -1, "stabilises": -1, "stabilizes": -1,
    "stabilising": -1, "stabilizing": -1, "stabilisation": -1,
    "stabilization": -1, "ceasefire": -1, "truce": -1, "withdraws": -1,
    "withdrawal": -1, "recedes": -1, "receding": -1, "cools": -1,
    "cooling": -1, "narrows": -1, "narrowing": -1, "resolves": -1,
}


# ---------------------------------------------------------------------------
# Cited mass — the DRIFTS denominator.
# ---------------------------------------------------------------------------

#: The three citation-marker shapes this tower actually writes: the unit's
#: ``[N]`` claim marker, the composition's ``[[ref:N]]`` ordinal and the
#: journal's ``[[ref:<uuid>]]``. Cited MASS is the count of DISTINCT markers a
#: row's body carries — a proxy for "how much evidence this read stands on",
#: computed the same way for every row in a group, which is all the drift test
#: needs (it compares a group against itself, never across desks).
_UUID_REF_RE = re.compile(r"\[\[ref:([0-9a-fA-F][0-9a-fA-F-]{7,35})\]\]")
_ORDINAL_REF_RE = re.compile(r"\[\[ref:(\d+)\]\]")
_CLAIM_MARKER_RE = re.compile(r"(?<!\[)\[(\d+)\](?!\])")


# ---------------------------------------------------------------------------
# The pure readers
# ---------------------------------------------------------------------------


def normalize(text: Any) -> str:
    """NFKC + casefold + punctuation-to-space + whitespace-collapse.

    The identical recipe ``spread_block._normalize_sentence`` applies, so a term
    key here and a framing key there mean the same thing by the same rule.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    raw = unicodedata.normalize("NFKC", raw).casefold()
    raw = _PUNCT_RE.sub(" ", raw)
    return " ".join(raw.split())


def cited_mass(body: Any) -> int:
    """Count of DISTINCT citation markers in a body — the drift test's
    denominator. Counts each marker SHAPE's distinct values and sums them, so a
    body mixing ``[3]`` and ``[[ref:3]]`` is not silently deduped across shapes
    that mean different things."""
    text = str(body or "")
    if not text:
        return 0
    uuids = {m.group(1).lower() for m in _UUID_REF_RE.finditer(text)}
    ordinals = {m.group(1) for m in _ORDINAL_REF_RE.finditer(text)}
    claims = {m.group(1) for m in _CLAIM_MARKER_RE.finditer(text)}
    return len(uuids) + len(ordinals) + len(claims)


def proper_candidates(title: Any) -> tuple[set[str], set[str]]:
    """``(mid_sentence, single_word_leads)`` proper-noun candidates for a title.

    THE LEAD PROBLEM, and why it is split rather than settled by a rule. A
    headline's FIRST word is capitalised because it starts a sentence, so its
    capitalisation carries no evidence — which is why ``Talks collapse in
    Geneva`` must not key on ``talks``. But a news headline's first word is also
    very often the actor (``Russia steps up strikes``), and a detector that
    dropped every lead would miss the single most likely genuine pattern in the
    corpus. Neither "always drop" nor "always keep" is right, so the lead is not
    decided here at all: it is returned SEPARATELY, and
    ``crossroads_detectors.detect_patterns`` admits it only when the SAME token
    also appears capitalised MID-SENTENCE somewhere else in the window's titles
    — corpus evidence that the capital is an identity, not a sentence boundary.

    A MULTI-WORD lead run (``Saudi Arabia steps up …``) is not a lead candidate
    at all: the second word's capital is already the evidence, so it goes
    straight into ``mid_sentence``.
    """
    raw = str(title or "").strip()
    if not raw:
        return set(), set()
    words = raw.split()
    if len(words) >= 2:
        capitalized = sum(1 for w in words if w[:1].isupper())
        if capitalized / len(words) >= TITLE_CASE_RATIO:
            # Title Case — capitalisation carries no information at all.
            return set(), set()

    mid: set[str] = set()
    leads: set[str] = set()
    for match in _PROPER_RUN_RE.finditer(raw):
        candidate = normalize(match.group(0))
        if not candidate:
            continue
        parts = candidate.split()
        if len(parts) > MAX_TERM_WORDS:
            continue
        if all(p in TERM_STOPWORDS for p in parts):
            continue
        if len(candidate) < MIN_TERM_CHARS:
            continue
        if match.start() == 0 and len(parts) == 1:
            leads.add(candidate)
        else:
            mid.add(candidate)
    return mid, leads


def commodity_terms(title: Any) -> set[str]:
    """The DECLARED commodity vocabulary this title carries.

    Matched over the NORMALISED title, so it is case-insensitive by
    construction — a Title-Cased headline still contributes its commodities
    even though it contributes no proper nouns.
    """
    padded = f" {normalize(title)} "
    return {term for term in COMMODITY_TERMS if f" {term} " in padded}


def title_terms(
    title: Any, *, confirmed_leads: frozenset[str] | set[str] = frozenset(),
) -> set[str]:
    """The entity + commodity terms one finding TITLE contributes.

    Precision guards, all deliberate: title only; NO proper-noun candidate from
    a Title-Cased headline; stopwords out; :data:`MIN_TERM_CHARS` floor;
    :data:`MAX_TERM_WORDS` ceiling; and a single-word LEAD keys only when
    ``confirmed_leads`` says the corpus saw that same token capitalised
    mid-sentence elsewhere (see :func:`proper_candidates`). The default empty
    ``confirmed_leads`` is the conservative answer — no lead keys — which is
    what a caller holding one title in isolation should get.
    """
    mid, leads = proper_candidates(title)
    return commodity_terms(title) | mid | (leads & set(confirmed_leads))


def title_polarity(title: Any) -> int:
    """``+1`` escalatory / ``-1`` de-escalatory / ``0`` no directional claim.

    The sign of the summed lexicon hits. A TIE is zero, never a coin flip: two
    desks whose titles both carry mixed direction are not a contradiction, and
    saying so costs a sentence the crossroads did not need to write.
    """
    normalized = normalize(title)
    if not normalized:
        return 0
    words = set(normalized.split())
    total = sum(weight for term, weight in POLARITY_LEXICON.items() if term in words)
    if total > 0:
        return 1
    if total < 0:
        return -1
    return 0


__all__ = [
    "COMMODITY_TERMS",
    "MAX_TERM_WORDS",
    "MIN_TERM_CHARS",
    "POLARITY_LEXICON",
    "TERM_STOPWORDS",
    "TITLE_CASE_RATIO",
    "cited_mass",
    "commodity_terms",
    "normalize",
    "proper_candidates",
    "title_polarity",
    "title_terms",
]
