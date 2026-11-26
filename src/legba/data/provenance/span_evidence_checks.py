# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SPAN → CITED-SOURCE checks: deterministic, no model, no DB.

The verify judge grades a claim against its citations and the assembly arms
prove a quotation is a verbatim slice of its desk head. Neither checks that the
NUMBERS a span states, or the ACTORS it is about, are in the sources it cites.
Measured 2026-11-11 (sweep_quality_1111b):

  * RU head 470c27a4 quoted into the world read: "162 drones … across eight
    Ukrainian regions [11][51][17][120]" — the cited CBC item (01f2c353) says
    "Russia attacked 10 Ukrainian regions" and "eight" only of RUSSIAN regions.
    Judge 1.00, quote fidelity 1.00.
  * Journal d4ff1266's lead: "162 drones" placed before a Times of India ref
    (0c4b6ba8) that carries no 162.
  * US head 4e230ea2's BLUF on US-Iran diplomacy cites a Zelensky / Russia-
    Ukraine item [18].
  * Journal d4ff1266's "four tanker kills": two of five cited items restate the
    other two, and every source says exploded / burning, none sunk or killed.

Four checks, each a pure function over (span text, cited texts):

  1. :func:`unsupported_numbers` — every cardinal in the span must occur in at
     least one cited text WITH the words that follow it ("eight Ukrainian
     regions" needs "eight|8 … Ukrainian … regions"; "eight Russian regions"
     does not count). A count the writer COMPUTED passes when it equals a count
     derived here (the distinct incidents or the refs cited by the clause).
  2. :func:`actor_mismatched_citations` — a citation must name one of the
     span's own actors; one that reaches the span only through the desk's own
     country while naming countries the span never mentions is about someone
     else.
  3. :func:`incident_groups` / :func:`distinct_incident_count` — cited items
     reporting the SAME incident (same quantity + same object + same place in
     the title) collapse to one; the count of events is the sum of each
     incident's own stated quantity.
  4. :func:`overstated_outcomes` — a terminal outcome word in the span
     ("sunk", "kills", "destroyed") needs a terminal outcome word in a cited
     text; "exploded / caught fire" does not license "kills".

Every check FLAGS. Nothing here removes text: callers mark the span and store
the flag on the published row.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence

__all__ = [
    "CHECKS_VERSION",
    "NumberMention",
    "actor_mismatched_citations",
    "clauses",
    "distinct_incident_count",
    "extract_numbers",
    "incident_groups",
    "number_supported",
    "overstated_outcomes",
    "strip_markers",
    "unsupported_numbers",
]

CHECKS_VERSION = "span_evidence_checks.v1"

# ---------------------------------------------------------------------------
# Tokenising
# ---------------------------------------------------------------------------

#: Every citation-marker spelling a span can carry: the journal's
#: ``[[ref:<uuid>]]`` and its ``[[spec]]`` family, the desk's ``[N]`` / ``【N】``.
_MARKER_RE = re.compile(r"\[\[[^\]]*\]\]|[\[【]\d{1,3}[\]】]")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b|\b[0-9a-f]{8}\b"
)
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
#: N-T (2026-11-13) — an ISO date or timestamp ("2026-11-13T09:56:26") is a
#: date, not three cardinals: crossroads b7a5b41d had its "11" and "09" marked.
_ISO_DATETIME_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
#: Identifiers, not quantities: ``country_g20_ru``, ``G20``, ``H200s``, ``F-16``.
_IDENT_RE = re.compile(r"\b\w+_\w+\b|\b[A-Za-z]+-?\d+\w*\b")
_TOKEN_RE = re.compile(
    r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?(?:st|nd|rd|th)?|%|[^\W\d_]+(?:[’'][^\W\d_]+)?",
    re.UNICODE,
)
_CLAUSE_BREAK_RE = re.compile(r"[,;:()—–]|\.\s|\s-\s")

_NUMBER_WORDS: dict[str, int] = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100, "dozen": 12,
}
#: "one" is deliberately absent: "one of", "no one", "one control regime" make
#: it a pronoun far more often than a count, and a flag on it would be noise.
_ORDINAL_WORDS: dict[str, int] = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
}
_MONTHS = frozenset({
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "jan", "feb", "mar", "apr",
    "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
})
#: A number right after one of these is an index, not a quantity: "day two",
#: "rank 17", "phase 3", "article 5".
_INDEX_PREFIXES = frozenset({
    "day", "days", "year", "week", "phase", "round", "tier", "rank", "level",
    "article", "section", "resolution", "no", "number", "page", "chapter",
    "grade", "category", "stage", "step", "item", "block", "lane", "wave",
    # The system's OWN instrument readings — a journal's self-report, checked
    # by the journal's instrument guards, never present in a news source.
    "intensity", "score", "salience", "brier", "bss", "confidence", "mass",
    "ratio", "weight",
})
#: "five days old", "303 hours ago": an age the writer computed from
#: timestamps, which no source states in that unit.
_DURATION_UNITS = frozenset({
    "second", "minute", "hour", "day", "week", "month", "year", "h", "d",
})
_DEMONYM_SUFFIX_RE = re.compile(r"(?:ian|ean|ese|ish|an|i)$")
_ESTIMATE_WORDS = frozenset({
    "likelihood", "chance", "probability", "odds", "risk", "confidence",
})
#: Magnitude words, normalised so "$6.3bn" and "6.3 billion" compare equal.
_MAGNITUDES: dict[str, str] = {
    "bn": "billion", "billion": "billion", "mn": "million", "million": "million",
    "tn": "trillion", "trillion": "trillion", "thousand": "thousand",
}
#: A count of the writer's OWN evidence — outlets, framings, sources, signals —
#: is an analytic tally over the citations, not a reported fact a source
#: could state; the journal tier passes it as a computed count where it can,
#: and the desk tier leaves it to the judge rather than flag every
#: "three outlets" in a narrative desk's BLUF.
_META_NOUNS = frozenset({
    "source", "outlet", "framing", "frame", "signal", "report", "item",
    "article", "citation", "desk", "read", "narrative", "theme", "account",
    "mention", "story", "headline", "post", "publication", "wire", "direction",
    "pattern", "line", "spread", "block", "channel", "voice", "strand", "thread",
    "cluster", "vector", "indicator", "lens", "track",
})
#: Words that end a quantity phrase.
_STOPWORDS = frozenset({
    "and", "or", "plus", "of", "in", "on", "at", "to", "from", "by", "with",
    "for", "the", "a", "an", "into", "across", "over", "under", "than", "as",
    "while", "after", "before", "since", "per", "this", "that", "which", "who",
    "overnight", "each", "more", "about", "nearly", "almost", "around", "were",
    "was", "are", "is", "be", "been", "had", "have", "has", "will", "would",
    "its", "their", "his", "her", "our", "them", "they", "it", "against",
    "toward", "towards", "through", "during", "within", "near", "off", "but",
    "not", "no", "so", "also", "then", "there", "here", "out", "up", "down",
    "again", "still", "already", "only", "just", "all", "both", "some",
    "other", "others", "another", "such", "these", "those", "times",
})
_PHRASE_WORDS = 2
_SOURCE_WINDOW = 4


def strip_markers(text: str) -> str:
    """``text`` with every citation marker, uuid, short hex id and clock time
    blanked to spaces — LENGTH-PRESERVING, so an offset into the result is an
    offset into the input."""
    out = str(text or "")
    for rx in (_MARKER_RE, _UUID_RE, _ISO_DATETIME_RE, _TIME_RE, _IDENT_RE):
        out = rx.sub(lambda m: " " * len(m.group(0)), out)
    return out


def _stem(word: str) -> str:
    w = word.lower().replace("’", "'")
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("es") and w[-3] in "sxz":
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


@dataclass(frozen=True)
class _Tok:
    raw: str
    low: str
    start: int
    end: int

    @property
    def is_word(self) -> bool:
        return self.raw[:1].isalpha()


def _tokens(text: str) -> list[_Tok]:
    clean = strip_markers(text)
    return [
        _Tok(m.group(0), m.group(0).lower(), m.start(), m.end())
        for m in _TOKEN_RE.finditer(clean)
    ]


def _numeric_value(tok: _Tok) -> tuple[float | None, str]:
    """``(value, kind)``; kind ∈ {cardinal, ordinal, ''}."""
    low = tok.low
    if low in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[low]), "cardinal"
    if low in _ORDINAL_WORDS:
        return float(_ORDINAL_WORDS[low]), "ordinal"
    if low[:1].isdigit():
        m = re.match(r"([\d,]+(?:\.\d+)?)(st|nd|rd|th)?$", low)
        if not m:
            return None, ""
        try:
            value = float(m.group(1).replace(",", ""))
        except ValueError:
            return None, ""
        return value, ("ordinal" if m.group(2) else "cardinal")
    return None, ""


# ---------------------------------------------------------------------------
# 1. Numbers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NumberMention:
    """One cardinal the span states, with the words that qualify it."""

    surface: str
    value: float
    phrase: tuple[str, ...]  # stemmed content words after the number
    start: int
    end: int  # end of the number + its phrase, offset into the span text

    @property
    def text(self) -> str:
        return " ".join((self.surface, *self.phrase)).strip()


def _gap_breaks(text: str, a: _Tok, b: _Tok) -> bool:
    return bool(_CLAUSE_BREAK_RE.search(text[a.end:b.start] + " "))


def extract_numbers(text: str) -> list[NumberMention]:
    """The CARDINALS a span asserts, minus dates, years, indices, ordinals,
    clock times and scores in [0, 1] (an instrument's number, not a reported
    fact — the journal's own instrument checks own those)."""
    clean = strip_markers(text)
    toks = _tokens(text)
    out: list[NumberMention] = []
    for i, tok in enumerate(toks):
        value, kind = _numeric_value(tok)
        if value is None or kind != "cardinal":
            continue
        if tok.low.isdigit() and len(tok.low) == 4 and 1900 <= value <= 2100:
            continue  # a year
        if "." in tok.low and 0.0 <= value <= 1.0:
            continue  # a score / probability
        prev = toks[i - 1] if i else None
        if prev is not None and prev.low in _INDEX_PREFIXES and not _gap_breaks(clean, prev, tok):
            continue
        if prev is not None and clean[prev.end:tok.start].strip() in {"n=", "="}:
            continue
        if clean[tok.end:tok.end + 1] in {"·", "="}:
            continue
        # A day of the month: "10 November", "November 10", "10-11 November".
        j = i + 1
        while j < len(toks) and _numeric_value(toks[j])[0] is not None and j - i <= 2:
            j += 1
        nxt = toks[j] if j < len(toks) else None
        if (nxt is not None and nxt.low in _MONTHS) or (
            prev is not None and prev.low in _MONTHS
        ):
            continue
        unit = toks[i + 1] if i + 1 < len(toks) else None
        if unit is not None and _stem(unit.low) in _DURATION_UNITS:
            continue  # a duration / horizon: "within 30 days", "five days old"
        phrase: list[str] = []
        end = tok.end
        last = tok
        # "211–204", "10-11": a score or a range — each side checked bare.
        joined = (
            (prev is not None and _numeric_value(prev)[0] is not None
             and clean[prev.end:tok.start].strip() in {"-", "–", "—", "/"})
            or (unit is not None and _numeric_value(unit)[0] is not None
                and clean[tok.end:unit.start].strip() in {"-", "–", "—", "/"})
        )
        for k in range(i + 1, min(len(toks), i + 1 + (0 if joined else _PHRASE_WORDS))):
            t = toks[k]
            if _gap_breaks(clean, last, t) or not (t.is_word or t.raw == "%"):
                break
            if t.low in _STOPWORDS or _numeric_value(t)[0] is not None:
                break
            if len(phrase) and (t.low.endswith("ing") or t.low.endswith("ed")):
                break
            if not phrase and t.low.endswith("ing") and len(t.low) > 5:
                break
            word = "percent" if t.raw == "%" else _MAGNITUDES.get(t.low, _stem(t.low))
            phrase.append(word)
            end = t.end
            last = t
            if word == "percent" or word in _MAGNITUDES.values():
                break  # "18.51% year-on-year", "$6.3bn Binance": the unit IS the phrase
            if len(phrase) == 1 and not _DEMONYM_SUFFIX_RE.search(t.low):
                # Only a demonym-like modifier needs its noun ("eight UKRAINIAN
                # regions" vs "eight Russian regions"); otherwise the first word
                # carries the quantity ("six major electricity substations",
                # "400 Mawozo gang members").
                break
        if phrase and phrase[0] in _META_NOUNS:
            continue  # a count of the desk's own evidence: "three outlets", "five framings"
        if phrase == ["percent"] and any(
            t.low in _ESTIMATE_WORDS for t in toks[i + 2:i + 5]
        ):
            continue  # the desk's own estimate: "a 15 percent likelihood"
        out.append(NumberMention(tok.raw, value, tuple(phrase), tok.start, end))
    return out


@functools.lru_cache(maxsize=64)
def _source_index(source: str) -> tuple[tuple[_Tok, ...], dict[float, tuple[int, ...]]]:
    """``(tokens, value → positions of that CARDINAL)`` for one source text.

    Cached (Q-2): a whole cited row is now read by the consult number check
    rather than its first 4,000 characters, and one row is checked against
    every number of every sentence that cites it — tokenised once, not once
    per number. Read-only: callers never mutate what this returns.
    """
    toks = tuple(_tokens(source))
    at: dict[float, list[int]] = {}
    for i, tok in enumerate(toks):
        value, kind = _numeric_value(tok)
        if value is not None and kind == "cardinal":
            at.setdefault(value, []).append(i)
    return toks, {v: tuple(ix) for v, ix in at.items()}


def number_supported(mention: NumberMention, source: str) -> bool:
    """Does ``source`` state ``mention`` — the same value followed, within
    :data:`_SOURCE_WINDOW` words, by every word of its phrase?"""
    toks, at = _source_index(str(source or ""))
    for i in at.get(mention.value, ()):
        if not mention.phrase:
            return True
        window = {
            ("percent" if t.raw == "%" else _MAGNITUDES.get(t.low, _stem(t.low)))
            for t in toks[i + 1:i + 1 + _SOURCE_WINDOW + len(mention.phrase)]
        }
        if all(w in window for w in mention.phrase):
            return True
    return False


#: ``[[ref:<uuid>]]`` OR ``[N]`` / ``【N】`` — a citation marker that NAMES a ref.
_REF_RE = re.compile(r"\[\[ref:([0-9a-fA-F-]{36})\]\]|[\[【](\d{1,3})[\]】]")


def clauses(text: str) -> list[tuple[int, int, list[str]]]:
    """``(start, end, refs)`` per clause: the text up to and including each run
    of adjacent citation markers, with the refs that run names. Text after the
    last marker run is a final clause carrying EVERY ref the text cites (an
    uncited tail is checked against the whole sentence's evidence)."""
    out: list[tuple[int, int, list[str]]] = []
    s = str(text or "")
    pos = 0
    run: list[str] = []
    run_end = None
    all_refs: list[str] = []
    for m in _REF_RE.finditer(s):
        ref = (m.group(1) or m.group(2) or "").lower()
        if ref and ref not in all_refs:
            all_refs.append(ref)
        if run_end is not None and s[run_end:m.start()].strip():
            out.append((pos, run_end, run))
            pos, run = run_end, []
        run.append(ref)
        run_end = m.end()
    if run_end is not None:
        out.append((pos, run_end, run))
        pos = run_end
    if s[pos:].strip():
        out.append((pos, len(s), list(all_refs)))
    return out


def unsupported_numbers(
    text: str,
    resolve: Callable[[str], str | None],
    *,
    computed_counts: Callable[[list[str]], Iterable[float]] | None = None,
) -> list[dict]:
    """Every cardinal in ``text`` that no text cited BY ITS CLAUSE states.

    ``resolve(ref)`` returns the cited item's text (``None`` when the ref does
    not resolve to a SOURCE — a prior read, an unresolved id; such refs neither
    support nor refute). A clause none of whose refs resolves is not checked:
    there is nothing to check it against, and an unresolvable ref is the
    citation floor's finding, not this one's.

    A marker may close the clause before it or open the one after it ("…in
    four regions [ToI] — six substations…"), so a number is checked against
    its own clause's refs AND the preceding clause's: the flag fires only when
    NEITHER neighbour states it — the d4ff1266 "162 drones [ToI]" shape, where
    the only adjacent source is one that carries no 162.

    ``computed_counts(refs)`` may return counts the writer could have derived
    from the citations themselves (distinct incidents, number of refs,
    distinct publishers); it is asked about the clause, the clause with its
    neighbour, and the whole text, and a number equal to one passes.
    """
    flagged: list[dict] = []
    parts = clauses(text)
    all_refs: list[str] = []
    for _s, _e, rs in parts:
        all_refs.extend(r for r in rs if r not in all_refs)
    for idx, (start, end, refs) in enumerate(parts):
        near = list(refs)
        if idx:
            near += [r for r in parts[idx - 1][2] if r not in near]
        texts = [t for t in (resolve(r) for r in near) if t]
        if not texts:
            continue
        derived: set[float] = set()
        if computed_counts is not None:
            for ref_set in (refs, near, all_refs):
                derived.update(float(x) for x in computed_counts(list(ref_set)))
        for mention in extract_numbers(text[start:end]):
            if any(number_supported(mention, t) for t in texts):
                continue
            if mention.value in derived:
                continue
            flagged.append({
                "number": mention.surface,
                "phrase": mention.text,
                "refs": list(refs),
                "start": start + mention.start,
                "end": start + mention.end,
            })
    return flagged


# ---------------------------------------------------------------------------
# 2. Actors
# ---------------------------------------------------------------------------


@dataclass
class ActorLexicon:
    """country key → its surfaces (casefolded names, case-sensitive abbrevs)."""

    names: dict[str, set[str]] = field(default_factory=dict)
    abbrevs: dict[str, set[str]] = field(default_factory=dict)
    demonyms: dict[str, set[str]] = field(default_factory=dict)


def lexicon_from_gazetteer(gz: object) -> ActorLexicon | None:
    """Build the actor lexicon from the profile gazetteer (``country_tokens``,
    ``slug_to_country``, ``slug_to_abbrev``, ``irregular_demonyms``). ``None``
    when the profile declares no country tokens — the actor check is then NOT
    APPLICABLE, which the caller states rather than passing."""
    try:
        tokens = set(gz.get("country_tokens") or ())  # type: ignore[attr-defined]
        slugs = dict(gz.get("slug_to_country") or {})  # type: ignore[attr-defined]
        abbrev = dict(gz.get("slug_to_abbrev") or {})  # type: ignore[attr-defined]
        irregular = dict(gz.get("irregular_demonyms") or {})  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — a profile without the tables
        return None
    if not tokens:
        return None
    lex = ActorLexicon()
    token_to_key: dict[str, str] = {}
    for slug, names in slugs.items():
        for n in names:
            token_to_key[str(n).casefold()] = str(slug)
    for t in tokens:
        key = token_to_key.get(t, t)
        lex.names.setdefault(key, set()).add(t)
        for d in irregular.get(t, ()) or ():
            lex.demonyms.setdefault(key, set()).add(str(d).casefold())
    for slug, forms in abbrev.items():
        if slug in lex.names:
            lex.abbrevs.setdefault(slug, set()).update(str(f) for f in forms)
    return lex


def actor_mentions(text: str, lex: ActorLexicon) -> dict[str, int]:
    """``{country key: mentions}`` in ``text`` — by name (with regular demonym
    suffixes), irregular demonym, or case-sensitive abbreviation."""
    raw = strip_markers(text)
    low = raw.casefold()
    found: dict[str, int] = {}
    for key, names in lex.names.items():
        n_hits = 0
        for n in names:
            # "Ukrainian" drops the name's final vowel before the suffix;
            # "Iranian" / "Israeli" / "Russian" keep it.
            stem = n[:-1] if len(n) > 4 and n[-1] in "aeiouy" else n
            if stem in low:
                n_hits += len(re.findall(
                    rf"(?<![a-z0-9])(?:{re.escape(n)}(?:n|an|ian|ese|i)?|"
                    rf"{re.escape(stem)}(?:ian|an|ese))(?![a-z0-9])",
                    low,
                ))
        for d in lex.demonyms.get(key, ()):
            if d in low:
                n_hits += len(re.findall(rf"(?<![a-z0-9]){re.escape(d)}(?![a-z0-9])", low))
        for a in lex.abbrevs.get(key, ()):
            n_hits += len(re.findall(rf"(?<![A-Za-z0-9]){re.escape(a)}(?![A-Za-z0-9])", raw))
        if n_hits:
            found[key] = n_hits
    return found


def actors_named(text: str, lex: ActorLexicon) -> set[str]:
    """The country keys ``text`` names (see :func:`actor_mentions`)."""
    return set(actor_mentions(text, lex))


def actor_mismatched_citations(
    span_text: str,
    citations: Sequence[tuple[str, str]],
    lex: ActorLexicon,
    *,
    self_key: str | None,
) -> list[dict]:
    """The citations that do not name the span's actors.

    ``citations`` is ``[(marker, cited text)]``. The span's actors are the
    countries it names plus the desk's own (``self_key``). The check applies
    only to a span that names a COUNTERPART — a country other than the desk's
    own ("the US-Iran confrontation"); a span about its own country alone is
    not judged, because context items about neighbours are ordinary there.

    A citation FAILS when it names none of the span's counterparts AND it is
    about other countries: some country the span never mentions is named more
    often in it than the desk's own country is — a Zelensky / Russia-Ukraine
    item on a US-Iran sentence, where the US appears once as the mediator. A
    citation naming a counterpart, or naming no country at all, passes.

    Precision first, measured on 250 live desk heads (2026-11-11): the looser
    "names any outsider" rule flagged 186 citations on 78 heads, most of them a
    North Korea item that also names South Korea; this rule flags 16 on 12.
    """
    span_actors = actors_named(span_text, lex)
    if self_key:
        span_actors.add(self_key)
    others = span_actors - ({self_key} if self_key else set())
    if not others:
        return []
    out: list[dict] = []
    for marker, text in citations:
        mentions = actor_mentions(text, lex)
        if not mentions or set(mentions) & others:
            continue
        outside = {k: v for k, v in mentions.items() if k not in span_actors}
        own = mentions.get(self_key, 0) if self_key else 0
        if not outside or max(outside.values()) <= own:
            continue
        out.append({
            "marker": marker,
            "names": sorted(mentions),
            "span_actors": sorted(span_actors),
        })
    return out


# ---------------------------------------------------------------------------
# 3. Incidents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Incident:
    value: float | None
    kind: str
    objects: frozenset[str]
    places: frozenset[str]
    title_words: frozenset[str]


def _incident(title: str) -> _Incident:
    toks = _tokens(title)
    value, kind, objects = None, "", frozenset()
    for i, t in enumerate(toks):
        v, k = _numeric_value(t)
        if v is not None and k:
            value, kind = v, k
            objects = frozenset(
                _stem(x.low) for x in toks[i + 1:i + 5]
                if x.is_word and x.low not in _STOPWORDS
            )
            break
    places = frozenset(
        t.low for j, t in enumerate(toks)
        if j and t.is_word and t.raw[:1].isupper() and t.low not in _NUMBER_WORDS
        and t.low not in _ORDINAL_WORDS and len(t.low) > 2
    )
    words = frozenset(_stem(t.low) for t in toks if t.is_word and t.low not in _STOPWORDS)
    return _Incident(value, kind, objects, places, words)


def same_incident(a_title: str, b_title: str) -> bool:
    """Do two cited items' TITLES report the same incident?

    Precision first: a near-identical title (word Jaccard >= 0.8), or the same
    stated quantity of the same KIND (two tankers = two tankers; the third
    tanker = the 3rd tanker) about an overlapping object in an overlapping
    place. Two titles with no quantity are never merged on topic alone — "a
    tanker was struck off Oman" and "a tanker exploded in Hormuz" may be two
    ships, and merging them would undercount.
    """
    a, b = _incident(a_title), _incident(b_title)
    if a.title_words and b.title_words:
        inter = len(a.title_words & b.title_words)
        union = len(a.title_words | b.title_words)
        if union and inter / union >= 0.8:
            return True
    if a.value is None or b.value is None:
        return False
    return (
        a.value == b.value and a.kind == b.kind
        and bool(a.objects & b.objects) and bool(a.places & b.places)
    )


def incident_groups(titles: Mapping[str, str]) -> list[list[str]]:
    """``{ref: title}`` → groups of refs reporting one incident, in first-seen
    order (singletons included)."""
    refs = list(titles)
    parent = {r: r for r in refs}

    def find(r: str) -> str:
        while parent[r] != r:
            parent[r] = parent[parent[r]]
            r = parent[r]
        return r

    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            if find(a) != find(b) and same_incident(titles[a], titles[b]):
                parent[find(b)] = find(a)
    groups: dict[str, list[str]] = {}
    for r in refs:
        groups.setdefault(find(r), []).append(r)
    return list(groups.values())


def distinct_incident_count(titles: Mapping[str, str]) -> int:
    """How many EVENTS the cited items report: one per incident group, or the
    group's own stated cardinal ("two tankers catch fire" is two)."""
    total = 0
    for group in incident_groups(titles):
        inc = _incident(titles[group[0]])
        if inc.kind == "cardinal" and inc.value and inc.value == int(inc.value):
            total += int(inc.value)
        else:
            total += 1
    return total


# ---------------------------------------------------------------------------
# 4. Outcome verbs
# ---------------------------------------------------------------------------

#: Terminal outcomes a span may only state when a cited source states one.
_TERMINAL_CLAIM = re.compile(
    r"\b(?:sunk|sank|sinks?|sinking|scuttled|destroy(?:ed|s)?|wrecked|"
    r"obliterated|razed|annihilated|kills?|killed|killing)\b",
    re.IGNORECASE,
)
#: What licenses one in a cited text: a terminal outcome of any kind, or a death.
_TERMINAL_EVIDENCE = re.compile(
    r"\b(?:sunk|sank|sinks?|sinking|scuttled|destroy(?:ed|s)?|destruction|"
    r"wrecked|obliterated|razed|annihilated|kills?|killed|killing|dead|died|"
    r"deaths?|fatalit(?:y|ies)|death toll|lost at sea)\b",
    re.IGNORECASE,
)


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _outcome_objects(clean: str, start: int, end: int) -> set[str]:
    """The content words within three tokens of an outcome word — what was
    sunk / killed / destroyed ("tanker" in "four tanker kills")."""
    toks = [t for t in _tokens(clean) if t.is_word]
    idx = [i for i, t in enumerate(toks) if t.start >= start and t.end <= end]
    if not idx:
        return set()
    lo, hi = max(0, idx[0] - 3), min(len(toks), idx[-1] + 4)
    return {
        _stem(t.low) for t in toks[lo:hi]
        if t.low not in _STOPWORDS and t.low not in _NUMBER_WORDS
        and t.low != "one" and not _TERMINAL_CLAIM.fullmatch(t.low)
        and len(t.low) > 2
    }


def overstated_outcomes(span_text: str, cited_texts: Sequence[str]) -> list[str]:
    """The terminal outcome words in ``span_text`` that no cited text licenses.

    A cited text licenses one when a SENTENCE of it states a terminal outcome
    and names the same object ("the destruction of US coastal radar" does not
    license "tanker kills"). ``[]`` when the span states none, or no cited text
    resolved (nothing to compare)."""
    clean = strip_markers(span_text)
    if not cited_texts:
        return []
    # Each terminal outcome a cited text states, with ITS object words.
    evidence: list[set[str]] = []
    for t in cited_texts:
        for s in _SENTENCE_RE.split(t or ""):
            for ev in _TERMINAL_EVIDENCE.finditer(s):
                evidence.append(_outcome_objects(s, ev.start(), ev.end()))
    out: set[str] = set()
    for m in _TERMINAL_CLAIM.finditer(clean):
        objects = _outcome_objects(clean, m.start(), m.end())
        licensed = any(not objects or not ev or objects & ev for ev in evidence)
        if not licensed:
            out.add(m.group(0).lower())
    return sorted(out)
