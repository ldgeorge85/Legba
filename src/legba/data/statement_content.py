# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Content readers for gate G2 — the numbers, dates and names a statement
states, and whether its evidence states them too.

Plan P1 §3 G2: "Every number and date in the statement text appears in a quote
or the cited observation; the subject's name or an alias is in a quote or the
signal title." This module is the reading half of that rule; the verdicts live
in :mod:`legba.data.statement_gate`.

WHAT COUNTS AS THE SAME NUMBER. Values, not spellings: ``1,200`` = ``1200``;
``1.2 million`` = ``1,200,000`` = ``1.2m``; ``3.2%`` = ``3.2 per cent``;
``five`` (evidence side) = ``5``. The evidence side also keeps each literal
mantissa (``1.2`` in ``1.2 million``), so a figure that appears literally in a
quote always matches. A digit run glued to letters (``G20``, ``F-16``,
``COVID-19``) is part of a name, not a stated number, and is not read.

DATES ARE NOT NUMBERS. A figure inside a date expression or a bare year is a
date part, never a quantity, on either side (:func:`date_spans`).

WHAT COUNTS AS THE SAME DATE. Day dates match on (month, day) and, when both
sides carry one, the year. A quote date without a year (``on 13 November``)
takes its year from the evidence's publication year when the caller supplies
it — the year a wire story leaves implicit. A month date (``September 2025``)
matches any date in that month. Weekday and relative words (``on Thursday``)
are not dates here: :mod:`legba.data.statement_dates` resolves them against a
publication or read time and holds the whole date rule.

Text is compared through the house fold (:mod:`legba.data.provenance.text_fold`)
— the one normalisation every comparator shares, full-width brackets included.

A LEAF: stdlib + the stdlib-only fold and citation-marker leaves.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .provenance.citation_markers import normalize_inline_citation_markers
from .provenance.text_fold import normalize_for_match

# ---------------------------------------------------------------------------
# Citation markers are not content
# ---------------------------------------------------------------------------

_MARKER_RE = re.compile(
    r"\[\[[^\]]*\]\]"                       # [[ref:…]] / [[find:…]]
    r"|\[\s*\d+(?:\s*[-–,]\s*\d+)*\s*\]"     # [3] [3-5] [3, 4]
    r"|\[\s*[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\s*\]"  # [uuid]
    r"|\[no[ _]citation\]",
    re.IGNORECASE,
)


def strip_markers(text: str | None) -> str:
    """``text`` with every citation marker removed (after the write-time
    marker normalisation, so ``【3】`` goes too). Markers carry ordinals, not
    claims: ``[21]`` is not the number 21."""
    if not text:
        return ""
    out = normalize_inline_citation_markers(text)
    out = _MARKER_RE.sub(" ", out)
    out = re.sub(r"[ \t]+([.,;:!?])", r"\1", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_MONTHS: dict[str, int] = {}
for _i, _names in enumerate(
    (
        ("january", "jan"), ("february", "feb"), ("march", "mar"),
        ("april", "apr"), ("may",), ("june", "jun"), ("july", "jul"),
        ("august", "aug"), ("september", "sep", "sept"), ("october", "oct"),
        ("november", "nov"), ("december", "dec"),
    ),
    start=1,
):
    for _n in _names:
        _MONTHS[_n] = _i
#: French month names (S-6): sources in that language date their copy
#: "le vendredi 13 novembre 2026". Read on both sides, like the English ones.
for _i, _names in enumerate(
    (
        ("janvier",), ("février", "fevrier"), ("mars",), ("avril",), ("mai",), ("juin",),
        ("juillet",), ("août", "aout"), ("septembre",), ("octobre",), ("novembre",),
        ("décembre", "decembre"),
    ),
    start=1,
):
    for _n in _names:
        _MONTHS.setdefault(_n, _i)

_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ORD = r"(?:st|nd|rd|th|er)?"

_ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DAY_MONTH_RE = re.compile(
    rf"(?<![\w])(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MONTH_ALT})\.?(?:,?\s+(\d{{4}}))?(?![\w])",
    re.IGNORECASE,
)
_MONTH_DAY_RE = re.compile(
    rf"(?<![\w])({_MONTH_ALT})\.?\s+(\d{{1,2}}){_ORD}(?:,?\s+(\d{{4}}))?(?![\w])",
    re.IGNORECASE,
)
_MONTH_YEAR_RE = re.compile(
    rf"(?<![\w])({_MONTH_ALT})\.?,?\s+(\d{{4}})(?![\w])", re.IGNORECASE,
)


@dataclass(frozen=True)
class DateMention:
    """One date stated in a text. ``day is None`` = month precision;
    ``year is None`` = the text left the year implicit."""

    year: int | None
    month: int
    day: int | None
    span: tuple[int, int]
    raw: str

    def label(self) -> str:
        y = f"{self.year:04d}" if self.year else "????"
        return f"{y}-{self.month:02d}" + (f"-{self.day:02d}" if self.day else "")


def _valid(month: int, day: int | None) -> bool:
    return 1 <= month <= 12 and (day is None or 1 <= day <= 31)


@dataclass(frozen=True)
class DateRange:
    """A date RANGE a text states ("11–12 October", "between 13 and 14
    November", "from November 18 to 19", "30 October–2 November"). Its
    endpoints are not single dates the text states (S-6)."""

    start: DateMention
    end: DateMention
    span: tuple[int, int]
    raw: str

    def label(self) -> str:
        return f"{self.start.label()}..{self.end.label()}"


_RANGE_PRE = r"(?:(?P<pre>between|from|du|entre(?:\s+le)?)\s+)?"
_RANGE_SEP = r"(?P<sep>[-–—]|to|until|till|through|and|au|et(?:\s+le)?)"
#: "11–12 October", "between 13 and 14 November", "du 10 au 30 novembre",
#: and the two-day LIST "13 and 14 November".
_DM_RANGE_RE = re.compile(
    rf"(?<![\w-]){_RANGE_PRE}(?P<d1>\d{{1,2}}){_ORD}\s*{_RANGE_SEP}\s*(?P<d2>\d{{1,2}}){_ORD}\s+"
    rf"(?:of\s+)?(?P<mon>{_MONTH_ALT})\.?(?:,?\s+(?P<y>\d{{4}}))?(?![\w])",
    re.IGNORECASE,
)
#: "November 18-19", "from November 18 to 19", "October 11 and 12".
_MD_RANGE_RE = re.compile(
    rf"(?<![\w-]){_RANGE_PRE}(?P<mon>{_MONTH_ALT})\.?\s+(?P<d1>\d{{1,2}}){_ORD}\s*{_RANGE_SEP}\s*"
    rf"(?P<d2>\d{{1,2}}){_ORD}(?:,?\s+(?P<y>\d{{4}}))?(?![\w])"
    # what follows must end the date: punctuation, the end, or a function
    # word — "October 11 and 12 people" is a date and a count, not two dates
    rf"(?=\s*(?:$|[.,;:)!?\]\[]|\s(?:in|at|on|when|as|and|to|with|for|after|before|while|during|"
    rf"amid|ahead|following|respectively|the|because|due|but|over|by|from)\b))",
    re.IGNORECASE,
)
_CROSS_GAP_RE = re.compile(r"^\s*(?:[-–—]|to|until|till|through|au)\s*$", re.IGNORECASE)
_CROSS_AND_RE = re.compile(r"^\s*(?:and|et(?:\s+le)?)\s*$", re.IGNORECASE)
_BETWEEN_BEFORE_RE = re.compile(r"(?<![\w-])(?:between|entre(?:\s+le)?)\s+$", re.IGNORECASE)


def _single_dates(text: str, taken: list[tuple[int, int]]) -> list[DateMention]:
    """The S-2 single-date reader (ISO, day-month, month-day, month-year),
    the longest reading of each span winning, outside ``taken``."""
    found: list[DateMention] = []

    def _free(span: tuple[int, int]) -> bool:
        return all(span[1] <= a or span[0] >= b for a, b in taken)

    def _add(m: re.Match[str], year: int | None, month: int, day: int | None) -> None:
        if not _valid(month, day) or not _free(m.span()):
            return
        found.append(DateMention(year, month, day, m.span(), m.group(0)))
        taken.append(m.span())

    for m in _ISO_DATE_RE.finditer(text):
        _add(m, int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for m in _DAY_MONTH_RE.finditer(text):
        _add(m, int(m.group(3)) if m.group(3) else None, _MONTHS[m.group(2).lower()],
             int(m.group(1)))
    for m in _MONTH_DAY_RE.finditer(text):
        _add(m, int(m.group(3)) if m.group(3) else None, _MONTHS[m.group(1).lower()],
             int(m.group(2)))
    for m in _MONTH_YEAR_RE.finditer(text):
        _add(m, int(m.group(2)), _MONTHS[m.group(1).lower()], None)
    found.sort(key=lambda d: d.span)
    return found


def _scan_dates(text: str) -> tuple[list[DateMention], list[DateRange]]:
    """``(singles, ranges)``. Ranges and two-day lists are read first; a
    range's endpoints are not singles; a list's days are."""
    singles: list[DateMention] = []
    ranges: list[DateRange] = []
    taken: list[tuple[int, int]] = []

    def _free(span: tuple[int, int]) -> bool:
        return all(span[1] <= a or span[0] >= b for a, b in taken)

    for rx in (_DM_RANGE_RE, _MD_RANGE_RE):
        for m in rx.finditer(text):
            if not _free(m.span()):
                continue
            month = _MONTHS[m.group("mon").lower()]
            d1, d2 = int(m.group("d1")), int(m.group("d2"))
            year = int(m.group("y")) if m.group("y") else None
            if not (_valid(month, d1) and _valid(month, d2)):
                continue
            sep = m.group("sep").lower()
            pre = (m.group("pre") or "").lower()
            is_list = sep == "and" or sep.startswith("et")
            if is_list and (pre.startswith("between") or pre.startswith("entre")):
                is_list = False
            if not is_list and d1 >= d2:
                continue
            # the span starts at the first day (a leading "between"/"from" is
            # not part of the date)
            start = m.start("d1") if rx is _DM_RANGE_RE else m.start("mon")
            span = (start, m.end())
            first = DateMention(year, month, d1, (m.start("d1"), m.end("d1")), m.group("d1"))
            last = DateMention(year, month, d2, (m.start("d2"), m.end()), text[m.start("d2"):m.end()])
            if is_list:
                singles += [first, last]
            else:
                ranges.append(DateRange(first, last, span, text[span[0]:span[1]]))
            taken.append(span)
    rest = _single_dates(text, taken)
    # cross-month ranges: two full dates joined by a dash / "to", or by
    # "and" after "between" ("from 30 October to 2 November")
    consumed: set[int] = set()
    for i in range(len(rest) - 1):
        a, b = rest[i], rest[i + 1]
        if a.day is None or b.day is None or i in consumed:
            continue
        gap = text[a.span[1]:b.span[0]]
        if _CROSS_GAP_RE.match(gap) or (_CROSS_AND_RE.match(gap)
                                        and _BETWEEN_BEFORE_RE.search(text[:a.span[0]])):
            ranges.append(DateRange(a, b, (a.span[0], b.span[1]), text[a.span[0]:b.span[1]]))
            consumed.update({i, i + 1})
    singles += [d for i, d in enumerate(rest) if i not in consumed]
    singles.sort(key=lambda d: d.span)
    ranges.sort(key=lambda r: r.span)
    return singles, ranges


def find_dates(text: str) -> list[DateMention]:
    """Every SINGLE date in ``text`` (ISO, day-month, month-day, month-year;
    each day of a two-day list "13 and 14 November"), in text order. A
    range's endpoints are not single dates (:func:`find_date_ranges`)."""
    return _scan_dates(text)[0]


def find_date_ranges(text: str) -> list[DateRange]:
    """Every date RANGE in ``text`` (S-6), in text order."""
    return _scan_dates(text)[1]


#: A bare YEAR: 1900–2099 in a year context — after a preposition or a period
#: word ("in 2025", "since 2019", "by end 2026", "mid-2025", "FY2026") — and
#: not part of a larger figure. A 4-digit figure outside such a context
#: ("2,034 cases", "2034 cases") stays a quantity. A year is a DATE part on
#: both sides: it never supports a quantity, nor a quantity it.
_YEAR_RE = re.compile(
    r"(?:(?<![\w-])(?:in|since|by|of|from|until|till|through|before|after|during|"
    r"early|late|end|start|year|fiscal|circa)\s+|(?<![\w-])mid-|(?<![\w-])FY\s?)"
    r"((?:19|20)\d{2})(?![\w%]|[.,]\d)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class YearMention:
    year: int
    span: tuple[int, int]
    raw: str


def find_years(text: str, *, skip: Sequence[tuple[int, int]] = ()) -> list[YearMention]:
    """Bare years outside ``skip`` spans (the day/month date spans)."""
    out: list[YearMention] = []
    for m in _YEAR_RE.finditer(text):
        span = m.span(1)
        if all(span[1] <= a or span[0] >= b for a, b in skip):
            out.append(YearMention(int(m.group(1)), m.span(1), m.group(1)))
    return out


def date_spans(text: str) -> list[tuple[int, int]]:
    """Every span of ``text`` that is a date part — day/month/ISO dates and
    bare years. The number reader skips these on BOTH sides (gate G2: a figure
    inside "4 April" is not the quantity 4)."""
    singles, ranges = _scan_dates(text)
    spans = [d.span for d in singles] + [r.span for r in ranges]
    spans += [y.span for y in find_years(text, skip=spans)]
    return sorted(spans)


def date_supported(
    stated: DateMention,
    evidence: Iterable[DateMention],
    *,
    implicit_years: Iterable[int] = (),
) -> bool:
    """Does any evidence date carry ``stated``?

    ``implicit_years`` are the years a yearless evidence date may take (the
    evidence's publication years)."""
    years = set(implicit_years)
    for ev in evidence:
        if ev.month != stated.month:
            continue
        if stated.day is not None and ev.day != stated.day:
            continue
        if stated.year is None or ev.year == stated.year:
            return True
        if ev.year is None and stated.year in years:
            return True
    return False


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

_SCALES = {
    "thousand": 1e3, "k": 1e3,
    "million": 1e6, "mn": 1e6, "m": 1e6,
    "billion": 1e9, "bn": 1e9, "b": 1e9,
    "trillion": 1e12, "tn": 1e12,
}
_AMBIGUOUS_SCALES = frozenset({"m", "b", "k"})
_NUMBER_RE = re.compile(
    r"(?<![\w.])(?<![A-Za-z]-)"
    r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?"
    r"(?:st|nd|rd|th)?"
    r"(?:\s*(thousand|million|billion|trillion|mn|bn|tn|k|m|b)(?![A-Za-z]))?",
    re.IGNORECASE,
)

#: Number words read on the EVIDENCE side (a source writes "five killed"; a
#: desk writes "5 killed").
_EVIDENCE_WORDS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100, "dozen": 12,
}
#: Number words read on the STATEMENT side. ``one`` is left out: "one report
#: says", "one of the" — a pronoun far more often than a count.
_STATEMENT_WORDS = {k: v for k, v in _EVIDENCE_WORDS.items() if k not in {"one", "zero"}}
_WORD_RE = re.compile(r"(?<![\w-])([a-z]+)(?![\w-])", re.IGNORECASE)


@dataclass(frozen=True)
class NumberMention:
    """One number stated in a text: its value (scale applied) and the literal
    mantissa (scale not applied)."""

    value: float
    literal: float
    raw: str
    span: tuple[int, int]
    #: The scale was a bare letter (``m``, ``b``, ``k``) — ``10 m`` may be ten
    #: metres, so the literal is accepted for it too.
    ambiguous_scale: bool = False


def _key(v: float) -> float:
    return round(v, 6)


def find_numbers(
    text: str, *, skip: Sequence[tuple[int, int]] = (), words: str = "statement",
) -> list[NumberMention]:
    """Every number in ``text`` outside the ``skip`` spans (date spans).

    ``words`` picks the number-word table: ``"statement"`` (no ``one``),
    ``"evidence"`` (all), or ``"none"``."""

    def _free(span: tuple[int, int]) -> bool:
        return all(span[1] <= a or span[0] >= b for a, b in skip)

    out: list[NumberMention] = []
    for m in _NUMBER_RE.finditer(text):
        if not _free(m.span()):
            continue
        whole = m.group(1).replace(",", "")
        literal = float(f"{whole}.{m.group(2)}" if m.group(2) else whole)
        scale_word = (m.group(3) or "").lower()
        value = literal * _SCALES.get(scale_word, 1.0)
        out.append(NumberMention(
            value, literal, m.group(0), m.span(), scale_word in _AMBIGUOUS_SCALES,
        ))
    table = {"statement": _STATEMENT_WORDS, "evidence": _EVIDENCE_WORDS}.get(words)
    if table:
        for m in _WORD_RE.finditer(text):
            n = table.get(m.group(1).lower())
            if n is not None and _free(m.span()):
                out.append(NumberMention(float(n), float(n), m.group(0), m.span()))
    out.sort(key=lambda n: n.span)
    return out


def evidence_number_keys(texts: Iterable[str], values: Iterable[float] = ()) -> set[float]:
    """The value set an evidence side states: every number OUTSIDE a date
    expression (value AND literal mantissa) and the observation values given.
    Date parts ("4" in "4 April", "2025") are not quantities and are not
    keys — the S-3 dry run's planted 2→4 passed on a quote's "4 April"."""
    keys: set[float] = {_key(float(v)) for v in values}
    for text in texts:
        for n in find_numbers(text, skip=date_spans(text), words="evidence"):
            keys.add(_key(n.value))
            keys.add(_key(n.literal))
    return keys


def number_supported(stated: NumberMention, keys: set[float]) -> bool:
    """Is the STATED value in the evidence keys? A stated magnitude
    (``1.2 million``) must be matched as a magnitude — ``1.2`` alone does not
    carry it — except for a bare-letter scale, which may be a unit."""
    if _key(stated.value) in keys:
        return True
    return stated.ambiguous_scale and _key(stated.literal) in keys


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

#: A name shorter than this must match as a whole word (``US``, ``EU``,
#: ``UK``); a longer one may carry a suffix (``Iran`` → ``Iranian``,
#: ``Oman`` → ``Omani``) but never a prefix (``Oman`` ≠ ``woman``).
SHORT_NAME_CHARS = 4


def name_present(name: str, text: str) -> bool:
    """Is ``name`` (or a suffixed form of it) a word-start match in ``text``,
    both folded?"""
    n = normalize_for_match(name)
    t = normalize_for_match(text)
    if not n or not t:
        return False
    tail = r"(?!\w)" if len(n) < SHORT_NAME_CHARS else ""
    return re.search(rf"(?<!\w){re.escape(n)}{tail}", t) is not None


__all__ = [
    "SHORT_NAME_CHARS",
    "DateMention",
    "DateRange",
    "NumberMention",
    "YearMention",
    "date_spans",
    "date_supported",
    "evidence_number_keys",
    "find_date_ranges",
    "find_dates",
    "find_numbers",
    "find_years",
    "name_present",
    "number_supported",
    "strip_markers",
]
