# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Windows stated in WORDS — the phrasings an asker actually types (lane Q-1) — pure.

``consult_window`` reads ISO dates and "last N hours/days". Most askers write
dates as words, so this leaf reads, in UTC:

* day-month and month-day dates, with or without ordinals and a year
  ("3 Nov", "3rd of November 2026", "November 3", "Nov. 3, 2026"); a date
  with no year is this year, or last year when that would put it in the future;
* ranges between them — "from 3 Nov to 10 Nov", "between 1 and 5 November",
  "3-10 Nov", "November 3 to 10" (a bare day borrows the other end's month);
  the end day is inclusive;
* "since <date>", "since Tuesday" (the most recent Tuesday before today),
  "since the 3rd" (this month's, or last month's when that is still to come);
* "on <date>" (that day), "in October" / "in October 2026" (that month);
* "the past fortnight", "the last couple of days", "the past few days" (3),
  "last weekend" / "this past weekend" (Saturday 00:00 to Monday 00:00),
  "last night" (yesterday 18:00 to today 06:00 UTC), "yesterday and today".

Nothing here reads a bare date as a window: a date counts only after a window
word (from / between / since / on / in) or as one end of a range, so "resolves
by 30 November" in a forecast never becomes the window.

Q-2 adds three phrasings, each returned with a READING — a sentence the
window lines print, so the asker sees how the words were read and can correct
them:

* NUMERIC ranges, "3/11 to 10/11", "03/11/2026 - 10/11/2026", "between 3/11
  and 10/11", and "since 3/11". A numeric date is ambiguous when both numbers
  are 12 or less. THE RULE (:data:`NUMERIC_RULE`): each reading — day/month
  and month/day — is kept only when it gives real calendar dates, a start on
  or before the end, and a start that is not in the future (a date with no
  year is this year, or last year when this year's would be in the future; an
  end before its start crosses into the next year, for a span of at most 92
  days, so two fractions never read as an eleven-month range; "3/4 of" is
  never a date). When both readings are
  valid, DAY/MONTH is taken; when one is, that one; when none is, the words
  are not a window. The reading taken — and the other one, when it was also
  valid — is stated.
* "from the 3rd to the 10th" / "between the 3rd and the 10th" (no month):
  this month when the 3rd has come, else last month; stated.
* "early / mid / late <month>" ("mid-November", "late Oct 2026", "early this
  month", "late last month"): FIXED day bands (:data:`PART_BANDS`) — early =
  days 1-10, mid = days 11-20, late = day 21 to the month's last day; a band
  still under way ends now; a band wholly in the future is not a window. The
  band is stated.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone

__all__ = ["NUMERIC_RULE", "PART_BANDS", "parse_words"]

#: The stated rule for an ambiguous numeric date (printed in the reading).
NUMERIC_RULE = (
    "a numeric date is read day/month unless only month/day gives real, "
    "past, start-before-end dates"
)
#: early / mid / late — fixed day bands; ``None`` = the month's last day.
PART_BANDS: dict[str, tuple[int, int | None]] = {"early": (1, 10), "mid": (11, 20), "late": (21, None)}

_MONTHS = {
    m: i for i, names in enumerate((
        ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
        ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
        ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december"),
    ), start=1) for m in names
}
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_ORD = r"(?:st|nd|rd|th)?"
_DM = rf"(\d{{1,2}}){_ORD}\s+(?:of\s+)?{_MON}(?:,?\s+(\d{{4}}))?"          # 3 Nov [2026]
_MD = rf"{_MON}\s+(\d{{1,2}}){_ORD}(?:,?\s+(\d{{4}}))?"                     # Nov 3[, 2026]
_DAY = rf"(\d{{1,2}}){_ORD}"
_SEP = r"\s*(?:to|through|thru|until|till|and|-|–|—)\s*"
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

_RANGES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"\b(?:from|between)?\s*{_DM}{_SEP}{_DM}", re.I), "dm-dm"),
    (re.compile(rf"\b(?:from|between)?\s*{_MD}{_SEP}{_MD}", re.I), "md-md"),
    (re.compile(rf"\b(?:from|between)?\s*{_DAY}{_SEP}{_DM}", re.I), "d-dm"),
    (re.compile(rf"\b(?:from|between)?\s*{_MD}{_SEP}{_DAY}\b(?!\s*(?:st|nd|rd|th)?\s*{_MON})", re.I), "md-d"),
)
_SINCE_DATE = (re.compile(rf"\bsince\s+(?:the\s+)?{_DM}", re.I), re.compile(rf"\bsince\s+{_MD}", re.I))
_SINCE_WEEKDAY = re.compile(r"\bsince\s+(?:last\s+)?(" + "|".join(_WEEKDAYS) + r")\b", re.I)
_SINCE_ORDINAL = re.compile(r"\bsince\s+the\s+(\d{1,2})(?:st|nd|rd|th)\b(?!\s+(?:of\s+)?" + _MON + ")", re.I)
_ON_DATE = (re.compile(rf"\bon\s+(?:the\s+)?{_DM}", re.I), re.compile(rf"\bon\s+{_MD}", re.I))
#: Capitalised only: "in may cases" is not May.
_IN_MONTH = re.compile(r"\bin\s+([A-Z][a-z]{2,8})\.?(?:\s+(\d{4}))?\b")
_WEEK_OF = (re.compile(rf"\bweek\s+of\s+(?:the\s+)?{_DM}", re.I), re.compile(rf"\bweek\s+of\s+{_MD}", re.I))
_MONTH_START = re.compile(r"\bsince\s+the\s+(?:start|beginning)\s+of\s+(?:the|this)\s+month\b", re.I)
#: "previous 48 hours" — HOURS only: "previous six months" names a baseline.
_PREVIOUS_HOURS = re.compile(r"\b(?:previous|preceding)\s+(\d{1,3})\s*(?:hours?|hrs?|h)\b", re.I)
_SPANS = (
    (re.compile(r"\b(?:last|past)\s+fortnight\b", re.I), timedelta(days=14)),
    (re.compile(r"\b(?:last|past)\s+couple\s+(?:of\s+)?days\b", re.I), timedelta(days=2)),
    (re.compile(r"\b(?:last|past)\s+few\s+days\b", re.I), timedelta(days=3)),
)
_WEEKEND = re.compile(r"\b(?:last|this\s+past|past|over\s+the)\s+weekend\b", re.I)
_LAST_NIGHT = re.compile(r"\blast\s+night\b", re.I)
_YESTERDAY_TODAY = re.compile(r"\byesterday\s+and\s+today\b", re.I)
#: A numeric date: "3/11", "03/11/2026", "3/11/26" — never part of a longer
#: slash run ("1/2/3/4") or a fraction inside a number.
_NUM = r"(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?"
_NUM_RANGE = re.compile(
    rf"(?<![\d/.])(?:\b(?:from|between)\s+)?{_NUM}{_SEP}{_NUM}(?![\d/])(?!\s+of\b)", re.I,
)
#: The longest span a year-less numeric range may take by crossing the new year.
_MAX_YEAR_CROSSING_DAYS = 92
_NUM_SINCE = re.compile(rf"\bsince\s+{_NUM}(?![\d/])", re.I)
_ORD_RANGE = re.compile(
    r"\b(?:from|between)\s+the\s+(\d{1,2})(?:st|nd|rd|th)" + _SEP
    + r"(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b(?!\s+(?:of\s+)?" + _MON + ")",
    re.I,
)
_PART = re.compile(
    r"\b(early|mid|late)(?:-|\s+)(?:in\s+|of\s+)?(?:" + _MON + r"(?:,?\s+(\d{4}))?\b"
    r"|(this|last)\s+month\b)",
    re.I,
)


def _month(word: str) -> int:
    key = word.lower().rstrip(".")
    return _MONTHS.get(key) or _MONTHS[key[:3]]


def _date(day: str, mon: str, year: str | None, now: datetime) -> date | None:
    try:
        y = int(year) if year else now.year
        d = date(y, _month(mon), int(day))
    except (ValueError, KeyError):
        return None
    if not year and d > now.date() + timedelta(days=1):
        d = d.replace(year=d.year - 1)
    return d


def _at(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _range(m: re.Match[str], kind: str, now: datetime) -> tuple[date, date] | None:
    g = m.groups()
    if kind == "dm-dm":
        a, b = _date(g[0], g[1], g[2], now), _date(g[3], g[4], g[5], now)
    elif kind == "md-md":
        a, b = _date(g[1], g[0], g[2], now), _date(g[4], g[3], g[5], now)
    elif kind == "d-dm":
        b = _date(g[1], g[2], g[3], now)
        a = _date(g[0], g[2], g[3] or (str(b.year) if b else None), now) if b else None
    else:  # md-d
        a = _date(g[1], g[0], g[2], now)
        b = _date(g[3], g[0], g[2] or (str(a.year) if a else None), now) if a else None
    if a is None or b is None or b < a:
        return None
    return a, b


def _d(d: date) -> str:
    return f"{d.day} {calendar.month_abbr[d.month]} {d.year}"


def _year(raw: str | None) -> int | None:
    if not raw:
        return None
    y = int(raw)
    return y + 2000 if y < 100 else y


def _numeric_reading(
    first: tuple[str, str], second: tuple[str, str] | None, years: tuple[int | None, int | None],
    order: str, today: date,
) -> tuple[date, date | None] | None:
    """One reading (``order`` = ``day/month`` | ``month/day``) of one or two
    numeric dates, or ``None`` when that reading is not a valid past range."""
    def parts(pair: tuple[str, str]) -> tuple[int, int]:
        a, b = int(pair[0]), int(pair[1])
        return (a, b) if order == "day/month" else (b, a)  # (day, month)

    no_year = years[0] is None and years[1] is None
    y1 = years[0] if years[0] is not None else years[1]
    y2 = years[1] if years[1] is not None else years[0]
    try:
        d1, m1 = parts(first)
        start = date(y1 or today.year, m1, d1)
        end = None
        if second is not None:
            d2, m2 = parts(second)
            end = date(y2 or today.year, m2, d2)
        if no_year:  # this year, or last year when this year's start is to come
            if end is not None and end < start:
                # "28/12 to 3/1" crosses the new year — a SHORT span only, so a
                # pair of fractions never reads as an eleven-month range.
                end = end.replace(year=end.year + 1)
                if (end - start).days > _MAX_YEAR_CROSSING_DAYS:
                    return None
            if start > today:
                start = start.replace(year=start.year - 1)
                end = end.replace(year=end.year - 1) if end is not None else None
    except ValueError:
        return None
    if start > today or (end is not None and end < start):
        return None
    return start, end


def _numeric(m: re.Match[str], *, ranged: bool, today: date) -> tuple[date, date | None, str] | None:
    """The numeric rule (:data:`NUMERIC_RULE`) over one match, with its reading."""
    g = m.groups()
    first = (g[0], g[1])
    second = (g[3], g[4]) if ranged else None
    years = (_year(g[2]), _year(g[5]) if ranged else None)
    got = {o: _numeric_reading(first, second, years, o, today) for o in ("day/month", "month/day")}
    valid = {o: r for o, r in got.items() if r is not None}
    if not valid:
        return None
    order = "day/month" if "day/month" in valid else "month/day"
    start, end = valid[order]

    def span(r: tuple[date, date | None]) -> str:
        return _d(r[0]) + (f" to {_d(r[1])}" if r[1] is not None else "")

    other = "month/day" if order == "day/month" else "day/month"
    if other in valid and valid[other] == valid[order]:
        why = "day/month and month/day read the same"
    elif other in valid:
        why = (f"{other} ({span(valid[other])}) was also valid; day/month is preferred — "
               "write the month as a word to choose the other")
    else:
        why = f"the {other} reading is not a valid past range"
    return start, end, f"read as {order}: {span((start, end))} ({why})"


def _ordinal_range(m: re.Match[str], today: date) -> tuple[date, date, str] | None:
    a, b = int(m.group(1)), int(m.group(2))
    first = today.replace(day=1)
    month = first if a <= today.day else (first - timedelta(days=1)).replace(day=1)
    try:
        start, end = month.replace(day=a), month.replace(day=b)
    except ValueError:
        return None
    if end < start:
        return None
    which = "this month" if month == first else "last month"
    return start, end, f"no month named, read as {which}: {_d(start)} to {_d(end)}"


def _part_of_month(m: re.Match[str], now: datetime) -> tuple[datetime, datetime, str] | None:
    """early / mid / late <month> on the fixed :data:`PART_BANDS`."""
    part = m.group(1).lower()
    today = now.date()
    if m.group(4):  # this / last month
        first = today.replace(day=1)
        if m.group(4).lower() == "last":
            first = (first - timedelta(days=1)).replace(day=1)
    else:
        mon = _month(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        first = date(year, mon, 1)
        if not m.group(3) and first.replace(day=PART_BANDS[part][0]) > today:
            first = first.replace(year=year - 1)
    lo, hi = PART_BANDS[part]
    last = calendar.monthrange(first.year, first.month)[1]
    start = first.replace(day=lo)
    end = first.replace(day=hi or last)
    if _at(start) >= now:
        return None  # wholly in the future: not a window on the record
    reading = (
        f"{part} {calendar.month_name[first.month]} {first.year} = days {lo}-{hi or last} "
        f"({_d(start)} to {_d(end)}; the fixed bands: early 1-10, mid 11-20, late 21 to month end)"
    )
    stop, reading = _inclusive_end(end, now, reading)
    return _at(start), stop, reading


def _inclusive_end(last: date, now: datetime, reading: str) -> tuple[datetime, str]:
    """The end of an inclusive last day; a range still under way ends now (said)."""
    end = _at(last) + timedelta(days=1)
    if end <= now:
        return end, reading
    return now, f"{reading}; the range is still under way, so the window ends now"


def _stated_reading(text: str, now: datetime) -> tuple[datetime, datetime, str, str] | None:
    """The Q-2 phrasings that carry a READING (see the module doc)."""
    today = now.date()
    m = _NUM_RANGE.search(text)
    if m:
        got = _numeric(m, ranged=True, today=today)
        if got and got[1] is not None:
            end, reading = _inclusive_end(got[1], now, got[2])
            return _at(got[0]), end, m.group(0).strip(), reading
    m = _ORD_RANGE.search(text)
    if m:
        got2 = _ordinal_range(m, today)
        if got2:
            end, reading = _inclusive_end(got2[1], now, got2[2])
            return _at(got2[0]), end, m.group(0).strip(), reading
    m = _PART.search(text)
    if m:
        got3 = _part_of_month(m, now)
        if got3:
            return got3[0], got3[1], m.group(0).strip(), got3[2]
    m = _NUM_SINCE.search(text)
    if m:
        got = _numeric(m, ranged=False, today=today)
        if got and _at(got[0]) < now:
            return _at(got[0]), now, m.group(0).strip(), got[2]
    return None


_ISO_DAY = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_ANY_DATE = (re.compile(rf"\b{_DM}", re.I), re.compile(rf"\b{_MD}\b", re.I))
_ANY_NUM = re.compile(rf"(?<![\d/.]){_NUM}(?![\d/])")


def date_in(text: str, *, now: datetime) -> tuple[date, str | None] | None:
    """A date written ANYWHERE in ``text`` (an event anchor's own words), with
    the numeric reading when it was a numeric date; ``None`` when none is."""
    m = _ISO_DAY.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))), None
        except ValueError:
            return None
    for i, rx in enumerate(_ANY_DATE):
        m = rx.search(text)
        if m:
            g = m.groups()
            d = _date(g[0], g[1], g[2], now) if i == 0 else _date(g[1], g[0], g[2], now)
            if d:
                return d, None
    m = _ANY_NUM.search(text)
    if m:
        got = _numeric(m, ranged=False, today=now.date())
        if got:
            return got[0], got[2]
    return None


def parse_words(
    text: str, *, now: datetime, late: bool = False,
) -> tuple[datetime, datetime, str] | tuple[datetime, datetime, str, str] | None:
    """``(start, end, basis[, reading])`` for a window stated in words, else ``None``.

    ``late=False`` reads ranges, "since", the named spans and the weekend/night
    forms; ``late=True`` reads the weakest forms — "on <date>", "in <month>" —
    which ``consult_window`` tries only after every other phrasing. A fourth
    element, the READING, rides only the Q-2 phrasings (numeric dates, a range
    of bare ordinals, early/mid/late) — how the words were read, for the asker.
    """
    if late:
        return _parse_late(text, now)
    stated = _stated_reading(text, now)
    if stated is not None:
        return stated
    for rx, kind in _RANGES:
        m = rx.search(text)
        if m:
            got = _range(m, kind, now)
            if got:
                return _at(got[0]), min(_at(got[1]) + timedelta(days=1), max(now, _at(got[1]))), m.group(0).strip()
    for rx in _SINCE_DATE:
        m = rx.search(text)
        if m:
            g = m.groups()
            d = _date(g[0], g[1], g[2], now) if rx is _SINCE_DATE[0] else _date(g[1], g[0], g[2], now)
            if d and _at(d) < now:
                return _at(d), now, m.group(0).strip()
    m = _SINCE_WEEKDAY.search(text)
    if m:
        target = _WEEKDAYS.index(m.group(1).lower())
        back = (now.weekday() - target) % 7 or 7
        return _at(now.date() - timedelta(days=back)), now, m.group(0).strip()
    m = _SINCE_ORDINAL.search(text)
    if m:
        day = int(m.group(1))
        try:
            d = now.date().replace(day=day)
            if d >= now.date():
                prev = (now.date().replace(day=1) - timedelta(days=1))
                d = prev.replace(day=day)
        except ValueError:
            d = None
        if d:
            return _at(d), now, m.group(0).strip()
    m = _MONTH_START.search(text)
    if m:
        return _at(now.date().replace(day=1)), now, m.group(0).strip()
    for rx in _WEEK_OF:
        m = rx.search(text)
        if m:
            g = m.groups()
            d = _date(g[0], g[1], g[2], now) if rx is _WEEK_OF[0] else _date(g[1], g[0], g[2], now)
            if d:
                return _at(d), min(_at(d) + timedelta(days=7), max(now, _at(d))), m.group(0).strip()
    m = _PREVIOUS_HOURS.search(text)
    if m:
        return now - timedelta(hours=int(m.group(1))), now, m.group(0).strip()
    for rx, span in _SPANS:
        m = rx.search(text)
        if m:
            return now - span, now, m.group(0).strip()
    m = _YESTERDAY_TODAY.search(text)
    midnight = _at(now.date())
    if m:
        return midnight - timedelta(days=1), now, m.group(0)
    m = _WEEKEND.search(text)
    if m:
        back = (now.weekday() - 5) % 7 or 7  # the most recent Saturday before today
        sat = midnight - timedelta(days=back)
        return sat, min(sat + timedelta(days=2), now), m.group(0)
    m = _LAST_NIGHT.search(text)
    if m:
        return midnight - timedelta(hours=6), min(midnight + timedelta(hours=6), now), m.group(0)
    return None


def _parse_late(text: str, now: datetime) -> tuple[datetime, datetime, str] | None:
    for rx in _ON_DATE:
        m = rx.search(text)
        if m:
            g = m.groups()
            d = _date(g[0], g[1], g[2], now) if rx is _ON_DATE[0] else _date(g[1], g[0], g[2], now)
            if d:
                return _at(d), _at(d) + timedelta(days=1), m.group(0).strip()
    m = _IN_MONTH.search(text)
    if m and m.group(1).lower() in _MONTHS:
        year = int(m.group(2)) if m.group(2) else now.year
        start = date(year, _month(m.group(1)), 1)
        if not m.group(2) and start > now.date():
            start = start.replace(year=year - 1)
        nxt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        return _at(start), min(_at(nxt), now), m.group(0).strip()
    return None
