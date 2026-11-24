# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gate G2's NUMBER rule (lane S-6): what a figure in a statement claims, and
which figures in a quote state it.

A desk writes "killed ten", "about 300", "18–19 November", "a 48-hour
strike", "rose 2 percentage points"; a source writes "dix personnes",
"some 300", "November 18 to 19", "48 hours", "2%". The S-2 reader compared
bare values, so the first and last pairs failed on spelling and an "about
300" was satisfied by any 300. This module reads each figure as a VALUE plus
what the text says about it — its qualifier, its unit, cardinal or ordinal,
single or range — and decides support on all of them.

WHAT IS READ (both sides unless said otherwise)
================================================

* Digits, grouped by commas (``1,200``) and, on the EVIDENCE side, by a
  space, no-break space or thin space (``73 000`` — French copy; the parts
  are kept as well, so nothing the S-2 reader found is lost). Magnitudes
  (``1.2 million``, ``3.5bn``) as before; a digit run glued to letters
  (``G20``, ``F-16``) is a name, not a figure.
* Number WORDS: ``two`` … ``ninety``, ``hundred``, ``dozen``, tens-units
  compounds (``twenty-five``) and a word with a scale (``two million``,
  ``three hundred``, ``a thousand``). On the STATEMENT side ``one`` is read
  only before a scale (``one million``) — alone it is usually a pronoun
  ("one report says", the S-2 rule, kept) — and ``zero`` is never read. The
  EVIDENCE side also reads ``one``, ``zero`` and the French words a source in
  that language writes (``deux`` … ``mille``; ``neuf`` and ``un``/``une``
  are left out: they are also "new" and an article).
* ORDINALS: ``3rd``, ``21st``, ``third`` … ``twentieth``; on the statement
  side ``first`` and ``second`` are not read (idiom and the unit of time).
  A fraction (``a third of``, ``two-thirds``) is not an ordinal.
* UNITS that change what a figure means: ``%`` / ``per cent`` / ``percent``
  (percent), ``percentage point(s)`` / ``pp`` (percentage points),
  ``basis points`` / ``bps`` (basis points), and a unit of TIME attached to
  the figure — ``48-hour``, ``three-day``, ``48 hours``, ``a 12-year-old``
  (an age, its own unit).
* QUALIFIERS immediately before the figure (or after it: ``300 or more``,
  ``300-plus``, ``or so``), in seven classes:

  =============  ================================================  =================
  class          words                                             values it states
  =============  ================================================  =================
  ``exact``      (none)                                            N
  ``approx``     about, around, roughly, approximately, some,      close to N, either
                 circa, an estimated, ``~``, N or so               side
  ``near_below`` nearly, almost, just under, close to              just below N
  ``near_above`` just over, slightly more than                     just above N
  ``below``      fewer than, less than, under, below               below N
  ``at_most``    up to, at most, no more than, as many as          N or below
  ``at_least``   at least, no fewer than, a minimum of, N or more  N or above
  ``above``      over, more than, above, upwards of, in excess     above N
                 of, exceeding, N-plus
  =============  ================================================  =================

  ``over`` before a figure with a unit of time ("over 3 days") means
  "during", not "more than": it is not a qualifier there.
* RANGES: ``A–B`` / ``A-B`` (a hyphen or dash between two figures with
  A < B — "19-14" and "2-1" are scores, two figures), ``between A and B``
  and ``A to B`` (but ``from A to B`` is a change, two figures). A unit or
  scale after B applies to A as well ("3-4%", "1.2-1.5 million").

THE RULE — a stated figure is SUPPORTED when an evidence figure (in a quote,
or a cited observation's value) has

1. the same VALUE (by value, not spelling; a quote's literal mantissa counts,
   as in S-2: "1.2" in "1.2 million"),
2. the same FORM (an ordinal is supported only by an ordinal; a count only by
   a count),
3. the same UNIT when the statement states one (a percent only by a percent,
   percentage points only by percentage points, "48-hour" only by 48 hours);
   a statement figure with no unit may be matched by a quote figure with one,
   as before. An observation's value has no unit and matches any.
4. a QUALIFIER whose values lie INSIDE the statement's — the quote implies
   the statement:

   ===============  ==================================================
   statement says   supported by a quote saying (same N)
   ===============  ==================================================
   N (exact)        N — never "about N", "over N", a range around N
   about N          N, about N, nearly N, just over N
   nearly N         nearly N
   just over N      just over N
   under N          under N, nearly N
   up to N          up to N, under N, nearly N, N
   at least N       at least N, over N, just over N, N
   over N           over N, just over N
   ===============  ==================================================

   The number is always the SAME number: "over 300" is never matched to a
   quote's "312" — an unrelated figure in the quote would satisfy it.
5. RANGES: a stated range is supported by the same range in a quote, or by
   BOTH endpoints stated in quotes as exact figures. A range never supports a
   single value — not one inside it, and not an endpoint: "300–400 died" does
   not state that 300 died. So a range's endpoints are not single figures on
   the evidence side.

Dates are not numbers (``statement_content.date_spans``, now including date
ranges and lists): a figure inside a date expression is skipped on both
sides, and the date rule (``statement_dates``) decides it.

PURE. stdlib + the content readers.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .statement_content import date_spans

EXACT = "exact"
APPROX = "approx"
NEAR_BELOW = "near_below"
NEAR_ABOVE = "near_above"
BELOW = "below"
AT_MOST = "at_most"
AT_LEAST = "at_least"
ABOVE = "above"
QUALIFIERS = (EXACT, APPROX, NEAR_BELOW, NEAR_ABOVE, BELOW, AT_MOST, AT_LEAST, ABOVE)

#: For each STATEMENT qualifier, the evidence qualifiers (same number) whose
#: values lie inside it — the table in the module doc.
IMPLIED_BY: dict[str, frozenset[str]] = {
    EXACT: frozenset({EXACT}),
    APPROX: frozenset({EXACT, APPROX, NEAR_BELOW, NEAR_ABOVE}),
    NEAR_BELOW: frozenset({NEAR_BELOW}),
    NEAR_ABOVE: frozenset({NEAR_ABOVE}),
    BELOW: frozenset({BELOW, NEAR_BELOW}),
    AT_MOST: frozenset({AT_MOST, BELOW, NEAR_BELOW, EXACT}),
    AT_LEAST: frozenset({AT_LEAST, ABOVE, NEAR_ABOVE, EXACT}),
    ABOVE: frozenset({ABOVE, NEAR_ABOVE}),
}

CARDINAL = "cardinal"
ORDINAL = "ordinal"

PERCENT = "percent"
PERCENTAGE_POINTS = "percentage_points"
BASIS_POINTS = "basis_points"
TIME_UNITS = ("minute", "hour", "day", "week", "month", "year")

# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

_UNITS_WORDS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS_WORDS: dict[str, int] = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}
_OTHER_WORDS: dict[str, int] = {"hundred": 100, "dozen": 12}
#: French cardinals a source in that language writes (evidence side only).
#: ``un``/``une`` (an article), ``neuf`` (also "new"), ``cent`` ("pour
#: cent") and ``mille`` are left out.
_FRENCH_WORDS: dict[str, int] = {
    "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8,
    "dix": 10, "onze": 11, "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15,
    "seize": 16, "vingt": 20, "trente": 30, "quarante": 40, "cinquante": 50,
    "soixante": 60,
}
_ORDINAL_WORDS: dict[str, int] = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "thirtieth": 30, "fortieth": 40, "fiftieth": 50,
    "hundredth": 100,
}
#: Ordinal words the STATEMENT side does not read ("at first", "first time";
#: "a second" of time).
_STATEMENT_SKIP_ORDINALS = frozenset({"first", "second"})

_SCALE_WORDS: dict[str, float] = {
    "hundred": 1e2, "thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12,
}
_SCALES = {
    "thousand": 1e3, "k": 1e3,
    "million": 1e6, "mn": 1e6, "m": 1e6,
    "billion": 1e9, "bn": 1e9, "b": 1e9,
    "trillion": 1e12, "tn": 1e12,
}
_AMBIGUOUS_SCALES = frozenset({"m", "b", "k"})

_DIGITS_RE = re.compile(
    r"(?<![\w.])(?<![A-Za-z]-)"
    r"(?P<whole>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<frac>\d+))?"
    r"(?P<ord>st|nd|rd|th)?"
    r"(?:\s*(?P<scale>thousand|million|billion|trillion|mn|bn|tn|k|m|b)(?![A-Za-z]))?",
    re.IGNORECASE,
)
#: Space-grouped thousands (evidence side): "73 000", "1 200 000".
_SPACED_RE = re.compile(r"(?<![\w.,])(\d{1,3}(?:[    ]\d{3})+)(?![\d,.]\d)")
_WORD_TOKEN_RE = re.compile(r"(?<![\w-])([a-z]+(?:-[a-z]+)*)(?![\w])", re.IGNORECASE)

_QUAL_BEFORE: tuple[tuple[str, str], ...] = (
    (NEAR_BELOW, r"nearly|almost|just\s+under|just\s+below|close\s+to|just\s+shy\s+of"),
    (NEAR_ABOVE, r"just\s+over|just\s+above|slightly\s+more\s+than|slightly\s+over|a\s+little\s+over"),
    (AT_MOST, r"up\s+to|at\s+most|no\s+more\s+than|not\s+more\s+than|a\s+maximum\s+of|as\s+many\s+as"),
    (AT_LEAST, r"at\s+least|no\s+fewer\s+than|no\s+less\s+than|not\s+less\s+than|a\s+minimum\s+of"),
    (BELOW, r"fewer\s+than|less\s+than|under|below"),
    (ABOVE, r"more\s+than|over|above|upwards\s+of|in\s+excess\s+of|exceeding"),
    (APPROX, r"about|around|roughly|approximately|approx\.?|some|circa|an\s+estimated|estimated"),
)
_QUAL_BEFORE_RE = [(cls, re.compile(rf"(?<![\w-])(?:{alts})\s*$", re.IGNORECASE))
                   for cls, alts in _QUAL_BEFORE]
_TILDE_RE = re.compile(r"~\s*$")
_QUAL_AFTER = (
    (AT_LEAST, re.compile(r"^(?:\s*\+|-plus\b|\s+or\s+more\b|\s+or\s+above\b)", re.IGNORECASE)),
    (AT_MOST, re.compile(r"^\s+or\s+(?:fewer|less|below)\b", re.IGNORECASE)),
    (APPROX, re.compile(r"^\s+or\s+so\b", re.IGNORECASE)),
)

_PCT_RE = re.compile(r"^\s*(?:%|per\s?cent\b|percent\b|pct\b|pour\s?cent\b)", re.IGNORECASE)
_PP_RE = re.compile(r"^\s*(?:percentage[\s-]+points?\b|pp\b|ppts?\b)", re.IGNORECASE)
_BP_RE = re.compile(r"^\s*(?:basis\s+points?\b|bps\b|bp\b)", re.IGNORECASE)
_TIME_ALT = "|".join(TIME_UNITS)
_TIME_RE = re.compile(rf"^(?:-|\s+)({_TIME_ALT})s?(-old)?\b", re.IGNORECASE)


@dataclass(frozen=True)
class Figure:
    """One figure a text states. A RANGE is one figure: ``value`` is its low
    end and ``high`` its high end."""

    value: float
    literal: float
    raw: str
    span: tuple[int, int]
    qualifier: str = EXACT
    unit: str | None = None
    form: str = CARDINAL
    high: float | None = None
    high_literal: float | None = None
    #: The scale was a bare letter (``m``, ``b``, ``k``) — ``10 m`` may be ten
    #: metres, so the literal is accepted for it too (as in S-2).
    ambiguous_scale: bool = False

    @property
    def is_range(self) -> bool:
        return self.high is not None

    def label(self) -> str:
        q = "" if self.qualifier == EXACT else f"{self.qualifier}:"
        v = f"{_fmt(self.value)}-{_fmt(self.high)}" if self.is_range else _fmt(self.value)
        u = f" {self.unit}" if self.unit else ""
        o = " (ordinal)" if self.form == ORDINAL else ""
        return f"{q}{v}{u}{o}"


def _fmt(v: float | None) -> str:
    if v is None:
        return "?"
    return str(int(v)) if float(v).is_integer() else str(v)


def _key(v: float | None) -> float | None:
    return None if v is None else round(float(v), 6)


@dataclass
class _Atom:
    value: float
    literal: float
    start: int
    end: int
    form: str = CARDINAL
    scale: str = ""
    ambiguous_scale: bool = False
    spaced: bool = False


def _free(span: tuple[int, int], skip: Sequence[tuple[int, int]]) -> bool:
    return all(span[1] <= a or span[0] >= b for a, b in skip)


def _word_value(token: str, side: str, following: str) -> tuple[float, str, int] | None:
    """``(value, form, consumed_chars)`` for a number-word token, or ``None``.

    Handles tens-units compounds ("twenty-five"), "N-<time unit>" compounds
    (the unit is read by the caller from the text after the number part)."""
    parts = token.lower().split("-")
    words = dict(_UNITS_WORDS)
    words.update(_TENS_WORDS)
    words.update(_OTHER_WORDS)
    if side == "evidence":
        words.update(_FRENCH_WORDS)
    head = parts[0]
    # tens-units: twenty-five
    if len(parts) >= 2 and head in _TENS_WORDS and parts[1] in _UNITS_WORDS and 0 < _UNITS_WORDS[parts[1]] < 10:
        value = _TENS_WORDS[head] + _UNITS_WORDS[parts[1]]
        used = len(head) + 1 + len(parts[1])
        rest = parts[2:]
    elif head in words:
        value = words[head]
        used = len(head)
        rest = parts[1:]
    elif head in _ORDINAL_WORDS and len(parts) == 1:
        if side == "statement" and head in _STATEMENT_SKIP_ORDINALS:
            return None
        if re.match(r"^\s+of\b", following, re.IGNORECASE):
            return None                       # "a third of" — a fraction
        return float(_ORDINAL_WORDS[head]), ORDINAL, len(head)
    else:
        return None
    if rest:
        # a hyphen compound is read only when the rest is a unit of time
        # ("three-day", "six-month-old"); "two-thirds", "one-off" are not figures
        if rest[0] not in TIME_UNITS and rest[0].rstrip("s") not in TIME_UNITS:
            return None
    if side == "statement" and (head == "zero" or (head == "one" and not rest)):
        return None                           # "one" alone: a pronoun (S-2 rule, kept)
    return float(value), CARDINAL, used


def _atoms(text: str, skip: Sequence[tuple[int, int]], side: str) -> list[_Atom]:
    atoms: list[_Atom] = []
    for m in _DIGITS_RE.finditer(text):
        if not _free(m.span(), skip):
            continue
        whole = m.group("whole").replace(",", "")
        literal = float(f"{whole}.{m.group('frac')}" if m.group("frac") else whole)
        scale = (m.group("scale") or "").lower()
        value = literal * _SCALES.get(scale, 1.0)
        form = ORDINAL if m.group("ord") and not m.group("frac") else CARDINAL
        atoms.append(_Atom(value, literal, m.start(), m.end(), form, scale, scale in _AMBIGUOUS_SCALES))
    if side == "evidence":
        for m in _SPACED_RE.finditer(text):
            if not _free(m.span(), skip) or not re.search(r"[    ]", m.group(1)):
                continue
            value = float(re.sub(r"\D", "", m.group(1)))
            atoms.append(_Atom(value, value, m.start(1), m.end(1), spaced=True))
    taken = [(a.start, a.end) for a in atoms]
    for m in _WORD_TOKEN_RE.finditer(text):
        if not _free(m.span(), skip) or not _free(m.span(), taken):
            continue
        token = m.group(1)
        got = _word_value(token, side, text[m.end():])
        if got is None:
            # "one million" — read on the statement side only before a scale
            if token.lower() in {"one", "a"}:
                sm = re.match(r"\s+(hundred|thousand|million|billion|trillion)\b", text[m.end():], re.IGNORECASE)
                if sm:
                    v = _SCALE_WORDS[sm.group(1).lower()]
                    atoms.append(_Atom(v, 1.0, m.start(), m.end() + sm.end()))
            continue
        value, form, used = got
        end = m.start() + used
        # a word scale after it: "two million", "three hundred"
        sm = re.match(r"\s+(hundred|thousand|million|billion|trillion)\b", text[end:], re.IGNORECASE) \
            if form == CARDINAL else None
        literal = value
        if sm and value < 1000:
            value = value * _SCALE_WORDS[sm.group(1).lower()]
            end = end + sm.end()
        atoms.append(_Atom(value, literal, m.start(), end, form))
    atoms.sort(key=lambda a: (a.start, -a.end))
    # drop an atom nested inside a longer one ("hundred" inside "three
    # hundred"); a space-grouped figure keeps its parts beside it on purpose
    out: list[_Atom] = []
    for a in atoms:
        nested = any(k.start <= a.start and a.end <= k.end and (k.start, k.end) != (a.start, a.end)
                     and not k.spaced for k in out)
        if not nested:
            out.append(a)
    return out


def _qualifier(text: str, start: int, end: int, unit: str | None) -> str:
    before = text[max(0, start - 40):start]
    after = text[end:end + 20]
    for cls, rx in _QUAL_AFTER:
        if rx.match(after):
            return cls
    if _TILDE_RE.search(before):
        return APPROX
    for cls, rx in _QUAL_BEFORE_RE:
        m = rx.search(before)
        if m:
            word = m.group(0).strip().lower()
            if cls == ABOVE and word == "over" and unit in TIME_UNITS:
                return EXACT        # "over 3 days" = during
            return cls
    return EXACT


def _unit(text: str, end: int) -> tuple[str | None, int]:
    """The unit right after a figure, and where it ends."""
    rest = text[end:end + 40]
    for name, rx in ((PERCENTAGE_POINTS, _PP_RE), (BASIS_POINTS, _BP_RE), (PERCENT, _PCT_RE)):
        m = rx.match(rest)
        if m:
            return name, end + m.end()
    m = _TIME_RE.match(rest)
    if m:
        unit = m.group(1).lower()
        return (f"{unit}_old" if m.group(2) else unit), end + m.end()
    return None, end


_RANGE_DASH_RE = re.compile(r"^\s*[-–—]\s*$")
_RANGE_TO_RE = re.compile(r"^\s+to\s+$", re.IGNORECASE)
_RANGE_AND_RE = re.compile(r"^\s+and\s+$", re.IGNORECASE)
_BETWEEN_RE = re.compile(r"(?<![\w-])between\s+$", re.IGNORECASE)
_FROM_RE = re.compile(r"(?<![\w-])from\s+$", re.IGNORECASE)


def read_figures(
    text: str, *, side: str = "statement", skip: Sequence[tuple[int, int]] | None = None,
) -> list[Figure]:
    """Every figure in ``text`` (outside date spans), read by the rule in the
    module doc. ``side`` is ``"statement"`` or ``"evidence"``."""
    if skip is None:
        skip = date_spans(text)
    every = _atoms(text, skip, side)
    spaced = [k for k in every if k.spaced]
    # the parts of a space-grouped figure are read as single figures only
    parts = [k for k in every if not k.spaced and any(s.start <= k.start and k.end <= s.end for s in spaced)]
    atoms = [k for k in every if k not in parts]
    figures: list[Figure] = []
    for k in parts:
        figures.append(Figure(k.value, k.literal, text[k.start:k.end], (k.start, k.end), EXACT, None,
                              k.form, None, None, k.ambiguous_scale))
    i = 0
    while i < len(atoms):
        a = atoms[i]
        b = atoms[i + 1] if i + 1 < len(atoms) else None
        if b is not None and a.form == CARDINAL and b.form == CARDINAL and b.start >= a.end:
            gap = text[a.end:b.start]
            before = text[max(0, a.start - 12):a.start]
            is_range = False
            if _RANGE_DASH_RE.match(gap) and gap.strip():
                is_range = True
            elif _RANGE_TO_RE.match(gap) and not _FROM_RE.search(before):
                is_range = True
            elif _RANGE_AND_RE.match(gap) and _BETWEEN_RE.search(before):
                is_range = True
            lo, hi = a.value, b.value
            lo_lit = a.literal
            if is_range and b.scale and not a.scale:
                lo = a.literal * _SCALES.get(b.scale, 1.0)
            if is_range and lo < hi:
                unit, uend = _unit(text, b.end)
                q = _qualifier(text, a.start, uend, unit)
                figures.append(Figure(lo, lo_lit, text[a.start:b.end], (a.start, b.end), q, unit,
                                      CARDINAL, hi, b.literal, a.ambiguous_scale or b.ambiguous_scale))
                i += 2
                continue
        unit, uend = _unit(text, a.end)
        q = _qualifier(text, a.start, uend, unit)
        figures.append(Figure(a.value, a.literal, text[a.start:a.end], (a.start, a.end), q, unit,
                              a.form, None, None, a.ambiguous_scale))
        i += 1
    return sorted(figures, key=lambda f: f.span)


@dataclass(frozen=True)
class EvidenceFigures:
    """What a statement's evidence states, as figures: every figure in every
    quote (or observation text) and every observation value."""

    figures: tuple[Figure, ...] = ()
    observation_values: tuple[float, ...] = ()
    detail: dict = field(default_factory=dict)


def evidence_figures(texts: Iterable[str], values: Iterable[float] = ()) -> EvidenceFigures:
    figs: list[Figure] = []
    for t in texts:
        figs.extend(read_figures(t or "", side="evidence"))
    return EvidenceFigures(tuple(figs), tuple(float(v) for v in values))


def _same(stated_value: float, stated_literal: float, ambiguous: bool, ev_value: float, ev_literal: float) -> bool:
    keys = {_key(ev_value), _key(ev_literal)}
    if _key(stated_value) in keys:
        return True
    return ambiguous and _key(stated_literal) in keys


def _unit_ok(stated: Figure, ev: Figure) -> bool:
    return stated.unit is None or stated.unit == ev.unit


def _single_supported(stated: Figure, ev: EvidenceFigures) -> bool:
    allowed = IMPLIED_BY.get(stated.qualifier, frozenset({EXACT}))
    for f in ev.figures:
        if f.is_range or f.form != stated.form or not _unit_ok(stated, f):
            continue
        if f.qualifier not in allowed:
            continue
        if _same(stated.value, stated.literal, stated.ambiguous_scale, f.value, f.literal):
            return True
    if stated.form == CARDINAL and EXACT in allowed:
        if any(_key(stated.value) == _key(v) for v in ev.observation_values):
            return True
    return False


def _endpoint(value: float, literal: float, stated: Figure) -> Figure:
    return Figure(value, literal, "", stated.span, EXACT, stated.unit, CARDINAL, None, None,
                  stated.ambiguous_scale)


def figure_supported(stated: Figure, ev: EvidenceFigures) -> bool:
    """The rule in the module doc, for one stated figure."""
    if not stated.is_range:
        return _single_supported(stated, ev)
    for f in ev.figures:
        if not f.is_range or not _unit_ok(stated, f):
            continue
        if _same(stated.value, stated.literal, stated.ambiguous_scale, f.value, f.literal) and \
                _same(stated.high, stated.high_literal or stated.high, stated.ambiguous_scale,
                      f.high, f.high_literal or f.high):
            return True
    lo = _endpoint(stated.value, stated.literal, stated)
    hi = _endpoint(stated.high, stated.high_literal or stated.high, stated)
    return _single_supported(lo, ev) and _single_supported(hi, ev)


def unsupported_figures(statement_text: str, ev: EvidenceFigures) -> tuple[list[Figure], list[Figure]]:
    """``(stated, missing)`` — every figure the statement text states, and
    those no evidence figure supports."""
    stated = read_figures(statement_text, side="statement")
    return stated, [f for f in stated if not figure_supported(f, ev)]


__all__ = [
    "ABOVE",
    "APPROX",
    "AT_LEAST",
    "AT_MOST",
    "BASIS_POINTS",
    "BELOW",
    "CARDINAL",
    "EXACT",
    "IMPLIED_BY",
    "NEAR_ABOVE",
    "NEAR_BELOW",
    "ORDINAL",
    "PERCENT",
    "PERCENTAGE_POINTS",
    "QUALIFIERS",
    "TIME_UNITS",
    "EvidenceFigures",
    "Figure",
    "evidence_figures",
    "figure_supported",
    "read_figures",
    "unsupported_figures",
]
