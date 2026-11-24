# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which dates a statement may carry — gate G2's date rule, decided in code.

THE RULE. A date a statement states (a day, a month, or a year) is accepted
for exactly one of these reasons, and the check records which:

(a) ``publication_day`` — it is the publication day of one of the statement's
    evidence rows. A wire report of a same-day event is dated by its
    publication; a desk writing that day is restating the row's own dateline,
    not adding a fact. ``relative_in_quote`` — it is a relative word in an
    evidence QUOTE ("on Thursday", "yesterday", "today", "last Monday")
    resolved against THAT row's publication time, deterministically:

    * ``today`` / ``tonight`` / ``this morning|afternoon|evening`` → the
      publication day; ``yesterday`` → the day before;
    * a weekday (``on Thursday``, ``Thursday's vote``) → its most recent
      occurrence on or before the publication day; ``last Thursday`` → its
      most recent occurrence strictly before;
    * ``next Thursday``, ``this week``, ``last week``, ``recently`` → NOT
      resolved: a forward reference or an interval states no day.

    A relative word in the STATEMENT is the desk's own clock, so it resolves
    the same way against the READ's time (``read_at``) and the resolved day
    must then pass (a) or (c); with no read time it cannot be checked and
    fails (``relative_date_unresolved``).

(b) ``as_of_frame`` — the statement frames itself with the READ's own day
    ("As of 14 November, …", "(as at 14 Nov)"). That is when the record
    LEARNED it, not when it was true: the frame is stripped from the date
    check and returned as ``known_at``. Only a frame whose date IS the read
    day is stripped; "as of 3 November" on a 14 November read is a claim
    about 3 November and is checked like any other date.

(c) ``in_quote`` — an evidence quote states it (a yearless quote date takes
    its row's publication year; a month date matches any day in it; a year
    matches a quote date or year in that year). A bare month in a period
    context ("in October", "since March") is a month date on both sides.

Anything else fails ``date_not_in_evidence``. No date is ever inferred from
context: not from the topic, not from the model's knowledge, not from the
desk's other sentences.

RANGES (S-6). A stated date RANGE ("11–12 October", "between 13 and 14
November", "from 30 October to 2 November") is accepted as ``in_quote_range``
when a quote states the same range, or as ``range_endpoints`` when BOTH its
endpoints pass the rule above (each by (a) or (c)). A range in a quote never
supports a single date — not one inside it and not an endpoint — so its
endpoints are not quote dates. A two-day LIST ("13 and 14 November",
"October 11 and 12") states both days: each is a single date on either side.

NUMBERS AND DATES DO NOT SUPPORT EACH OTHER. A figure inside a date
expression ("4 April", "2026-11-13") or a bare year ("in 2025") is a DATE
part, never a quantity: the S-3 dry run let a planted 2→4 pass because the
quote said "4 April". :func:`statement_content.date_spans` gives every date span (day/month
dates and bare years) so the number reader skips them on BOTH sides; a bare
quantity in a quote likewise never supports a stated date.

PURE. stdlib + the content readers.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from .statement_content import (
    DateMention,
    DateRange,
    date_supported,
    find_date_ranges,
    find_dates,
    find_years,
)

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_WEEKDAY_ALT = "|".join(_WEEKDAYS)
_RELATIVE_RE = re.compile(
    rf"(?<![\w])(?:(?P<mod>last|next|this|on|since|by|until|early|late)\s+)?"
    rf"(?P<word>{_WEEKDAY_ALT}|today|tonight|yesterday)"
    rf"(?:\s+(?P<part>morning|afternoon|evening|night))?(?![\w])",
    re.IGNORECASE,
)
_THIS_PART_RE = re.compile(r"(?<![\w])this\s+(?:morning|afternoon|evening)(?![\w])", re.IGNORECASE)
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December")
#: A bare month in a period context ("in October", "since March", "late May"):
#: capitalised full name, no day or year attached (those are day/month dates).
_BARE_MONTH_RE = re.compile(
    r"(?<![\w-])(?:in|since|during|by|until|through|from|early|late|mid-|end of|start of|last|"
    r"this|of|In|Since|During|By|Until|From|Early|Late|Last|This)\s*"
    rf"({'|'.join(_MONTH_NAMES)})(?![\w])(?!\s*,?\s*\d)"
)
_AS_OF_RE = re.compile(r"\(?\b(?:as\s+of|as\s+at|as\s+on)\s+", re.IGNORECASE)


@dataclass(frozen=True)
class ResolvedRelative:
    """A relative date word resolved against an anchor time."""

    raw: str
    span: tuple[int, int]
    day: date | None   # None = stated but not resolvable to a day


def _utc_day(value: datetime | date) -> date:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.date()
    return value


def resolve_relative(text: str, anchor: datetime | date | None) -> list[ResolvedRelative]:
    """Relative day words in ``text`` resolved against ``anchor`` (rule (a)
    above). Unresolvable forms are returned with ``day=None``."""
    out: list[ResolvedRelative] = []
    base = _utc_day(anchor) if anchor is not None else None
    for m in _RELATIVE_RE.finditer(text):
        mod = (m.group("mod") or "").lower()
        word = m.group("word").lower()
        day: date | None = None
        if base is not None and mod != "next":
            if word in {"today", "tonight"}:
                day = base
            elif word == "yesterday":
                day = base - timedelta(days=1)
            elif mod != "this":
                back = (base.weekday() - _WEEKDAYS.index(word)) % 7
                if mod == "last" and back == 0:
                    back = 7
                day = base - timedelta(days=back)
        out.append(ResolvedRelative(m.group(0), m.span(), day))
    for m in _THIS_PART_RE.finditer(text):
        if not any(r.span[0] <= m.start() < r.span[1] for r in out):
            out.append(ResolvedRelative(m.group(0), m.span(), base))
    return sorted(out, key=lambda r: r.span)


def all_dates(text: str) -> list[DateMention]:
    """:func:`find_dates` plus bare months in a period context ("in
    October": month precision, year implicit)."""
    found = find_dates(text)
    taken = [d.span for d in found] + [r.span for r in find_date_ranges(text)]
    for m in _BARE_MONTH_RE.finditer(text):
        span = m.span(1)
        if all(span[1] <= a or span[0] >= b for a, b in taken):
            found.append(DateMention(None, _MONTH_NAMES.index(m.group(1)) + 1, None, span, m.group(1)))
    return sorted(found, key=lambda d: d.span)


def find_as_of_frames(text: str) -> list[DateMention]:
    """Dates that open an as-of frame ("as of 14 November", "(as at
    2026-11-14)") — the date immediately following the frame words."""
    out: list[DateMention] = []
    dates = find_dates(text)
    for m in _AS_OF_RE.finditer(text):
        for d in dates:
            if d.span[0] == m.end():
                out.append(d)
    return out


@dataclass(frozen=True)
class EvidenceDating:
    """One evidence row's dating inputs: its quote and publication time."""

    quote: str
    published_at: datetime | date | None


@dataclass(frozen=True)
class DateVerdict:
    """The date rule's answer for one statement."""

    stated: tuple[str, ...]
    missing: tuple[str, ...]
    accepted: dict
    known_at: date | None
    unresolved_relative: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.missing and not self.unresolved_relative


def _day_matches(d: date, mention: DateMention, implicit_years: set[int]) -> bool:
    if mention.month != d.month:
        return False
    if mention.day is not None and mention.day != d.day:
        return False
    if mention.year is None:
        return d.year in implicit_years or not implicit_years
    return mention.year == d.year


def _mention_of(d: date, raw: str) -> DateMention:
    return DateMention(d.year, d.month, d.day, (0, 0), raw)


def judge_dates(
    text: str,
    evidence: Iterable[EvidenceDating],
    *,
    read_at: datetime | date | None = None,
    allow_publication_date: bool = True,
) -> DateVerdict:
    """Apply the rule to a statement's text (markers already stripped)."""
    rows = list(evidence)
    pub_days = [_utc_day(r.published_at) for r in rows if r.published_at is not None]
    years = {p.year for p in pub_days}
    quote_dates: list[DateMention] = []
    quote_ranges: list[DateRange] = []
    quote_years: set[int] = set()
    relative_days: list[date] = []
    for r in rows:
        q = r.quote or ""
        found = all_dates(q)
        quote_dates.extend(found)
        quote_ranges.extend(find_date_ranges(q))
        quote_years.update(d.year for d in found if d.year)
        if r.published_at is not None:
            pub_year = _utc_day(r.published_at).year
            quote_years.update(pub_year for d in found if d.year is None)
        quote_years.update(y.year for y in find_years(q, skip=[d.span for d in found]))
        relative_days.extend(x.day for x in resolve_relative(q, r.published_at) if x.day is not None)
    read_day = _utc_day(read_at) if read_at is not None else None

    stated_dates = all_dates(text)
    frames = [d for d in find_as_of_frames(text)
              if read_day is not None and _day_matches(read_day, d, {read_day.year})]
    frame_spans = {d.span for d in frames}
    accepted: dict[str, list[str]] = {}
    missing: list[str] = []

    def _accept(reason: str, raw: str) -> None:
        accepted.setdefault(reason, []).append(raw)

    def _judge_day(m: DateMention) -> str | None:
        if date_supported(m, quote_dates, implicit_years=years):
            return "in_quote"
        if any(_day_matches(rd, m, years) for rd in relative_days):
            return "relative_in_quote"
        if allow_publication_date and m.day is not None and any(
                _day_matches(p, m, years) for p in pub_days):
            return "publication_day"
        return None

    for m in stated_dates:
        if m.span in frame_spans:
            _accept("as_of_frame", m.raw.rstrip("."))
            continue
        why = _judge_day(m)
        if why:
            _accept(why, m.raw.rstrip("."))
        else:
            missing.append(m.raw.rstrip("."))
    stated_ranges = find_date_ranges(text)

    def _same_end(a: DateMention, b: DateMention) -> bool:
        if a.month != b.month or a.day != b.day:
            return False
        if a.year is None or b.year is None:
            other = a.year if a.year is not None else b.year
            return other is None or other in years or not years
        return a.year == b.year

    for r in stated_ranges:
        if any(_same_end(r.start, q.start) and _same_end(r.end, q.end) for q in quote_ranges):
            _accept("in_quote_range", r.raw)
        elif _judge_day(r.start) and _judge_day(r.end):
            _accept("range_endpoints", r.raw)
        else:
            missing.append(r.raw)
    for y in find_years(text, skip=[d.span for d in stated_dates] + [r.span for r in stated_ranges]):
        if y.year in quote_years:
            _accept("in_quote", y.raw)
        else:
            missing.append(y.raw)
    unresolved: list[str] = []
    for rel in resolve_relative(text, read_day):
        if rel.day is None:
            if read_day is None:
                unresolved.append(rel.raw)
            continue   # "next Tuesday" / "this week": states no day, nothing to check
        why = _judge_day(_mention_of(rel.day, rel.raw))
        if why:
            _accept(f"relative_in_statement:{why}", rel.raw)
        else:
            missing.append(rel.raw)
    stated = tuple(d.label() for d in stated_dates) + tuple(r.label() for r in stated_ranges)
    return DateVerdict(stated, tuple(missing), accepted,
                       read_day if frames else None, tuple(unresolved))


def valid_from_stated(
    valid_from: datetime | date,
    precision: str,
    text: str,
    evidence: Iterable[EvidenceDating],
    *,
    read_at: datetime | date | None = None,
) -> str | None:
    """Why a typed ``valid_from`` is a date the sentence or its evidence
    states (``None`` = it is not: the seat inferred it). ``month`` precision
    matches any stated date in that month."""
    vf = _utc_day(valid_from)
    rows = list(evidence)
    pub_days = [_utc_day(r.published_at) for r in rows if r.published_at is not None]
    candidates: list[tuple[str, date | DateMention]] = []
    candidates += [("statement", d) for d in all_dates(text)]
    candidates += [("statement", d) for rg in find_date_ranges(text) for d in (rg.start, rg.end)]
    candidates += [("quote", d) for r in rows for d in all_dates(r.quote or "")]
    candidates += [("quote", d) for r in rows for rg in find_date_ranges(r.quote or "")
                   for d in (rg.start, rg.end)]
    candidates += [("relative_in_quote", x.day) for r in rows
                   for x in resolve_relative(r.quote or "", r.published_at) if x.day]
    read_day = _utc_day(read_at) if read_at is not None else None
    candidates += [("relative_in_statement", x.day) for x in resolve_relative(text, read_day) if x.day]
    candidates += [("publication_day", p) for p in pub_days]
    years = {p.year for p in pub_days} | ({read_day.year} if read_day else set())
    for why, c in candidates:
        if isinstance(c, DateMention):
            if c.month != vf.month or (c.year is not None and c.year != vf.year) or (
                    c.year is None and vf.year not in years):
                continue
            if precision == "month" or c.day is None or c.day == vf.day:
                if precision == "day" and c.day is None:
                    continue
                return why
        else:
            if c.year == vf.year and c.month == vf.month and (precision == "month" or c.day == vf.day):
                return why
    return None


__all__ = [
    "DateVerdict",
    "EvidenceDating",
    "ResolvedRelative",
    "all_dates",
    "find_as_of_frames",
    "judge_dates",
    "resolve_relative",
    "valid_from_stated",
]
